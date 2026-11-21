# Spark Auto (`learn.eltngl.com`)

Automated Selenium exercise solver for **National Geographic Learning (NGL) Spark** assignments (`Avallain Buckminster` interactive player).

## How It Works
1. **Isolated Browser Session ([account_session.py](account_session.py))**:
   - Launches Headless Chrome with an account-isolated profile directory and signs in to `https://learn.eltngl.com/dashboard/assignments`.
2. **Assignment Scanner & Resume Runner ([run_account.py](run_account.py))**:
   - Scans the student's assignment dashboard, skips already completed (`100%`) or exhausted-attempt activities, and works through each remaining exercise sequentially.
   - Automatically handles activity launch overlays, multi-page activities, and final score reporting (`completion_report.json`).
3. **Live Interaction Solver ([solver.py](solver.py))**:
   - Inspects the live Avallain Buckminster interaction engine inside the activity iframe and solves all 9 exercise interaction types automatically:
     - Multiple Choice (`choice`)
     - Text Entry / Fill-in-the-blank (`textentry`)
     - Inline Choice / Dropdown (`inlinechoice`)
     - Drag & Drop / Gap Match (`gapmatch`)
     - Match / Pairing (`match`)
     - Order / Sequencing (`order`)
     - Categorize / Grouping (`categorize`)
     - Hotspot / Image Selection (`hotspot`)
     - Highlight / Select Text (`highlight`)
4. **Pre-Submit 100% Guard ([answer_guard.py](answer_guard.py))**:
   - Checks the live DOM/engine state before clicking Submit to confirm `100%` accuracy (`revealed == 0`, zero unanswered or wrong items).
5. **Queue Worker ([cloud_worker.py](cloud_worker.py))**:
   - Headless queue runner configured via environment variables for running assignments on demand via GitHub Actions ([.github/workflows/cloud_worker.yml](.github/workflows/cloud_worker.yml)).

## Local Usage
```bash
pip install -r requirements.txt
python3 run_account.py <username>
```

## Files
- [solver.py](solver.py): Live Avallain Buckminster solver covering all 9 interaction types.
- [answer_guard.py](answer_guard.py): Pre-submit 100% verification guard.
- [run_account.py](run_account.py): Dashboard scanner, resume handler, and completion reporter.
- [account_session.py](account_session.py): Account-isolated Selenium Chrome session helper.
- [cloud_worker.py](cloud_worker.py): Headless queue worker entry point.
- [requirements.txt](requirements.txt): Pinned Python dependencies.
- [.github/workflows/cloud_worker.yml](.github/workflows/cloud_worker.yml): On-demand GitHub Actions runner workflow.
