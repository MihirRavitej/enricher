# Facebook Contact Enricher - Derive phone number and email id of agents by name and dealership

## Files
- `fb_contact_enricher.py`: Playwright-based script to find public phone/email on Facebook pages for each agent.
- `requirements.txt`: Minimal dependencies.

## Quick start
1) Create a Python 3.12 virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
2) Install deps and Playwright browser:
   ```bash
   pip install -r requirements.txt
   playwright install
   ```
3) Put your CSV/XLS file (must include columns "Agent Name" and "Dealership") in the same folder.
4) Run:
   ```bash
   python fb_contact_enricher.py -i <input_filename>.csv -o <output>_enriched.csv --headful
   ```
   Add `--manual` if you want to choose among multiple Facebook results per agent.

## Output
- CSV with two extra columns: `Phone Number`, `Email ID`. Values are `NA` if not found.
- A log CSV `<output>_log.csv` with search query, chosen candidate URL, and notes to encourage QA.

## Tips for higher hit-rate
- Keep dealership names precise.
- Facebook sometimes shows a login wall. Even then, the script often extracts text from the loaded DOM. If it fails, re-run specific rows with `--manual`.

## Respect site policies
This script reads only publicly visible information. Add random delays by default. Use responsibly.
