"""Firebase-backed cloud Chrome worker using scoped Service Account auth."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from google.auth.transport.requests import Request
from google.oauth2 import service_account
import requests

from account_session import open_session, session_folder
from run_account import run_remaining

SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/firebase.database",
]


class FirebaseAdminClient:
    def __init__(self):
        self.base_url = os.environ.get("FIREBASE_RTDB_BASE", "").strip().rstrip("/")
        if not self.base_url:
            raise RuntimeError("Missing FIREBASE_RTDB_BASE environment variable")
        raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
        if not raw:
            raise RuntimeError("Missing FIREBASE_SERVICE_ACCOUNT environment variable")
        info = json.loads(raw) if raw.startswith("{") else json.loads(Path(raw).read_text())
        self.creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    def get_token(self) -> str:
        if not self.creds.valid:
            self.creds.refresh(Request())
        return self.creds.token

    def get(self, path: str):
        r = requests.get(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.get_token()}"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def put(self, path: str, payload):
        r = requests.put(
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {self.get_token()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def patch(self, path: str, payload):
        r = requests.patch(
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {self.get_token()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def delete(self, path: str):
        r = requests.delete(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.get_token()}"},
            timeout=20,
        )
        r.raise_for_status()


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SAFE_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
SAFE_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{4,32}$")


def load_crypto_secrets():
    raw_pem = os.environ.get("INBOX_PRIVATE_KEY", "").strip().replace("\\n", "\n")
    if not raw_pem:
        raise RuntimeError("Missing INBOX_PRIVATE_KEY environment variable")
    private_key = serialization.load_pem_private_key(raw_pem.encode("utf-8"), password=None)

    hmac_secret = os.environ.get("HMAC_SECRET_KEY", "").strip()
    if not hmac_secret:
        raise RuntimeError("Missing HMAC_SECRET_KEY environment variable")
    return private_key, hmac_secret


def decrypt_password(private_key, enc_b64: str) -> str:
    ciphertext = base64.b64decode(enc_b64, validate=True)
    plaintext = private_key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return plaintext.decode("utf-8")


def compute_account_key(hmac_secret: str, username: str) -> str:
    payload = f"spark-v3:{username.lower()}".encode("utf-8")
    return hmac.new(hmac_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def cleanup_stale_public_jobs(fb: FirebaseAdminClient, exclude_job_id: str | None = None):
    now = time.time()
    jobs_map = fb.get("/ngl_spark_public/jobs.json") or {}
    if not isinstance(jobs_map, dict):
        return
    for jid, j in list(jobs_map.items()):
        if not SAFE_PUBLIC_ID_RE.fullmatch(str(jid)):
            continue
        if (
            jid != exclude_job_id
            and isinstance(j, dict)
            and j.get("status") in ("running", "queued")
        ):
            last_ts = _safe_float(j.get("updated_at") or j.get("created_at"), now)
            if now - last_ts > 1800:
                fb.patch(
                    f"/ngl_spark_public/jobs/{jid}.json",
                    {
                        "status": "failed",
                        "updated_at": now,
                        "detail": "หมดเวลาเชื่อมต่อ กรุณากดยืนยันเพื่อทำต่อจากเดิมอีกครั้ง",
                    },
                )
    if len(jobs_map) > 30:
        ordered = sorted(
            jobs_map.items(),
            key=lambda kv: _safe_float((kv[1] or {}).get("created_at"), 0.0),
        )
        for old_jid, _ in ordered[:-30]:
            if SAFE_PUBLIC_ID_RE.fullmatch(str(old_jid)):
                fb.delete(f"/ngl_spark_public/jobs/{old_jid}.json")


def update_public_job(fb: FirebaseAdminClient, job_id: str, **fields):
    if not SAFE_PUBLIC_ID_RE.fullmatch(str(job_id)):
        raise ValueError("Invalid public job_id")
    fields["updated_at"] = time.time()
    fb.patch(f"/ngl_spark_public/jobs/{job_id}.json", fields)
    cleanup_stale_public_jobs(fb, exclude_job_id=job_id)


def increment_completed_stats(fb: FirebaseAdminClient):
    stats = fb.get("/ngl_spark_public/stats.json") or {}
    count = int(stats.get("completed_count") or 0) + 1
    fb.put("/ngl_spark_public/stats.json", {"completed_count": count})


def process_one_job(fb: FirebaseAdminClient, private_key, hmac_secret: str) -> bool:
    inbox = fb.get("/ngl_spark_private/inbox.json")
    if not isinstance(inbox, dict) or not inbox:
        return False

    items = []
    for k, v in inbox.items():
        valid_item = (
            isinstance(v, dict)
            and SAFE_ID_RE.fullmatch(str(k))
            and v.get("job_id") == k
            and v.get("verified") is True
            and isinstance(v.get("username"), str)
            and SAFE_USERNAME_RE.fullmatch(v["username"])
            and isinstance(v.get("encrypted_password"), str)
            and 100 <= len(v["encrypted_password"]) <= 1024
        )
        if valid_item:
            items.append((k, v))
        elif SAFE_PUBLIC_ID_RE.fullmatch(str(k)):
            try:
                fb.delete(f"/ngl_spark_private/inbox/{k}.json")
            except Exception:
                pass
    if not items:
        return False

    items.sort(key=lambda kv: _safe_float(kv[1].get("created_at"), 0.0))
    job_id, job = items[0]
    username = job["username"]
    enc_password = job["encrypted_password"]
    display_name = str(job.get("display_name") or "ผู้ใช้งาน").strip()[:32] or "ผู้ใช้งาน"

    # Claim the job and immediately delete encrypted credentials from inbox.
    fb.delete(f"/ngl_spark_private/inbox/{job_id}.json")
    print("[worker] Claimed 1 job", flush=True)

    try:
        password = decrypt_password(private_key, enc_password)
        if not password or len(password) > 64:
            raise ValueError("Invalid decrypted password length")
    except Exception as exc:
        print(f"[worker] Rejected undecryptable job: {type(exc).__name__}", flush=True)
        fb.delete(f"/ngl_spark_public/jobs/{job_id}.json")
        return True

    salted_key = compute_account_key(hmac_secret, username)

    existing_completed = fb.get(f"/ngl_spark_private/completed/{salted_key}.json")
    if isinstance(existing_completed, dict) and existing_completed.get("completed"):
        fb.delete(f"/ngl_spark_public/jobs/{job_id}.json")
        print("[worker] Skipped already-completed job", flush=True)
        return True

    fb.put(f"/ngl_spark_private/active/{salted_key}.json", {"updated_at": time.time()})
    update_public_job(
        fb,
        job_id,
        display_name=display_name,
        status="running",
        detail="กำลังเปิดเบราว์เซอร์และตรวจสอบแบบฝึกหัด...",
    )

    driver = None
    folder = session_folder(username)
    try:
        def on_progress(done_count, total_count, msg):
            fb.put(f"/ngl_spark_private/active/{salted_key}.json", {"updated_at": time.time()})
            update_public_job(
                fb,
                job_id,
                display_name=display_name,
                status="running",
                done_activities=done_count,
                total_activities=total_count,
                detail=msg,
            )

        driver, folder = open_session(username, password=password, persist_session=False)
        run_remaining(
            driver,
            folder,
            username,
            progress_callback=on_progress,
            persist_session=False,
            quiet_logs=True,
        )

        rep_path = folder / "completion_report.json"
        total_done = 45
        graded_full = 45
        if rep_path.exists():
            try:
                rep_data = json.loads(rep_path.read_text())
                total_done = int(rep_data.get("completed", 45))
                graded_full = int(rep_data.get("graded_full_score", total_done))
            except Exception:
                pass

        done_msg = (
            f"ทำแบบฝึกหัดครบ {total_done}/{total_done} ข้อ (100%)"
            if graded_full == total_done
            else f"ทำข้อที่เหลือสำเร็จแล้ว ({graded_full}/{total_done} ข้อได้ 100%, ข้าม {total_done - graded_full} ข้อที่ใช้สิทธิ์ทำครบแล้ว)"
        )

        already_marked = fb.get(f"/ngl_spark_private/completed/{salted_key}.json")
        fb.put(
            f"/ngl_spark_private/completed/{salted_key}.json",
            {"completed": True, "completed_at": time.time()},
        )
        if not already_marked:
            increment_completed_stats(fb)

        update_public_job(
            fb,
            job_id,
            display_name=display_name,
            status="completed",
            done_activities=total_done,
            total_activities=total_done,
            detail=done_msg,
            completed_at=time.time(),
        )
        print(f"[worker] Completed job ({graded_full}/{total_done})", flush=True)
    except Exception as exc:
        print(f"[worker] Failed job: {type(exc).__name__}", flush=True)
        update_public_job(
            fb,
            job_id,
            display_name=display_name,
            status="failed",
            detail="เกิดข้อผิดพลาดระหว่างทำแบบฝึกหัด กรุณากดยืนยันเพื่อลองใหม่อีกครั้ง",
        )
    finally:
        try:
            fb.delete(f"/ngl_spark_private/active/{salted_key}.json")
        except Exception:
            pass
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
    return True


def main():
    parser = argparse.ArgumentParser(description="Firebase inbox Chrome worker for Spark Auto")
    parser.add_argument("--loop", action="store_true", help="Run continuously until interrupted")
    parser.add_argument("--stay-alive", type=int, default=0, help="Poll for N seconds before exiting")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between inbox polls")
    args = parser.parse_args()

    fb = FirebaseAdminClient()
    private_key, hmac_secret = load_crypto_secrets()
    try:
        cleanup_stale_public_jobs(fb)
    except Exception:
        pass

    worker_started = time.monotonic()
    deadline = worker_started + args.stay_alive if args.stay_alive > 0 else None

    while True:
        if not args.loop and time.monotonic() - worker_started > 7200:
            print("[worker] Reached 2-hour soft limit between jobs; exiting cleanly.", flush=True)
            break
        did_work = process_one_job(fb, private_key, hmac_secret)
        if not args.loop and (deadline is None or time.monotonic() >= deadline):
            if not did_work:
                break
        if not did_work:
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
