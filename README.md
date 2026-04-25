# GRUL11 Mailer

Minimal daily mailer for Icatu Vanguarda GRUL11 documents.

It fetches `https://www.icatuvanguarda.com.br/produtos/grul11`, parses the
static HTML table with BeautifulSoup, and emails only:

- Relatorios Gerenciais
- Fatos Relevantes

Sent documents are tracked in `sent_documents.json`.
Each PDF is sent in its own email. The subject and attachment filename use:

```text
YYYY-MM-DD GRUL11 [Type of Report]
YYYY-MM-DD GRUL11 [Type of Report].pdf
```

## Setup

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` with:

```bash
RESEND_API_KEY=...
RESEND_FROM_EMAIL=...
RESEND_TO_EMAIL=...
```

`RESEND_TO_EMAIL` may contain one email or multiple comma-separated emails.

## Run

```bash
python main.py --dry-run
python main.py
```

The first real run sends every historical matching document, one email per PDF.
Later runs send only documents missing from `sent_documents.json`.

If Icatu returns a temporary error for one PDF, the script retries that download,
continues with the remaining documents, and keeps successful sends in
`sent_documents.json` so the failed PDF can be retried on the next run.

## GitHub Actions

Add these repository secrets or variables:

- `RESEND_API_KEY`
- `RESEND_FROM_EMAIL`
- `RESEND_TO_EMAIL`

The workflow runs weekdays at `0 14 * * 1-5`, which is 11:00 in Sao Paulo.
