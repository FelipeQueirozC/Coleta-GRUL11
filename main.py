from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

FUND_URL = "https://www.icatuvanguarda.com.br/produtos/grul11"
STATE_PATH = Path(__file__).with_name("sent_documents.json")
TARGET_TYPES = {"relatorios gerenciais", "fatos relevantes"}
REQUEST_TIMEOUT = 45
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_SECONDS = 5
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_EMAIL_BASE64_BYTES = 35 * 1024 * 1024
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GRUL11Mailer/1.0)",
}


@dataclass(frozen=True)
class Document:
    id: str
    doc_type: str
    date_br: str
    date_iso: str
    title: str
    url: str


@dataclass(frozen=True)
class DownloadedDocument:
    document: Document
    filename: str
    content: bytes
    content_base64: str
    sha256: str


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return " ".join(without_accents.casefold().split())


def parse_br_date(value: str) -> str:
    return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()


def document_id(doc_type: str, date_iso: str, url: str) -> str:
    return f"{normalize_text(doc_type)}|{date_iso}|{url}"


def clean_title(value: str) -> str:
    title = " ".join(value.replace(".", " ").split())
    if title.casefold().startswith("pdf "):
        title = title[4:]
    return title or "Documento"


def document_mail_name(document: Document) -> str:
    return f"{document.date_iso} GRUL11 {document.title}"


def attachment_filename(document: Document) -> str:
    return f"{document_mail_name(document)}.pdf"


def fetch_documents(session: requests.Session) -> list[Document]:
    response = session.get(FUND_URL, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    documents: dict[str, Document] = {}

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        link = row.find("a", href=True)
        if len(cells) < 3 or link is None:
            continue

        doc_type = cells[1].get_text(" ", strip=True)
        if normalize_text(doc_type) not in TARGET_TYPES:
            continue

        date_br = cells[2].get_text(" ", strip=True)
        date_iso = parse_br_date(date_br)
        url = urljoin(FUND_URL, link["href"])
        title = clean_title(link.get_text(" ", strip=True))
        doc_id = document_id(doc_type, date_iso, url)

        documents[doc_id] = Document(
            id=doc_id,
            doc_type=doc_type,
            date_br=date_br,
            date_iso=date_iso,
            title=title,
            url=url,
        )

    if not documents:
        raise RuntimeError("No GRUL11 target documents were found on the Icatu page.")

    return sorted(
        documents.values(),
        key=lambda item: (item.date_iso, item.doc_type, item.url),
        reverse=True,
    )


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {"sent": []}

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sent"), list):
        raise ValueError(f"{path} must contain an object with a 'sent' list.")
    return data


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def sent_ids(state: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for record in state.get("sent", []):
        record_id = record.get("id")
        if isinstance(record_id, str):
            ids.add(record_id)
    return ids


def append_sent_record(
    state: dict[str, Any],
    sent_document: DownloadedDocument,
    resend_email_id: str,
) -> None:
    existing = {
        record["id"]: record
        for record in state.get("sent", [])
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    sent_at = datetime.now(timezone.utc).isoformat()

    document = sent_document.document
    existing[document.id] = {
        "id": document.id,
        "type": document.doc_type,
        "date": document.date_br,
        "date_iso": document.date_iso,
        "title": document.title,
        "url": document.url,
        "filename": sent_document.filename,
        "sha256": sent_document.sha256,
        "sent_at": sent_at,
        "resend_email_id": resend_email_id,
    }

    state["sent"] = sorted(
        existing.values(),
        key=lambda record: (record.get("date_iso", ""), record.get("type", "")),
        reverse=True,
    )


def download_document(
    session: requests.Session, document: Document
) -> DownloadedDocument:
    response: requests.Response | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            response = session.get(
                document.url,
                headers=HTTP_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            if (
                response.status_code in TRANSIENT_STATUS_CODES
                and attempt < DOWNLOAD_RETRIES
            ):
                print(
                    f"Download got HTTP {response.status_code}; retrying "
                    f"{attempt}/{DOWNLOAD_RETRIES} after {DOWNLOAD_RETRY_SECONDS}s: "
                    f"{document.url}"
                )
                sleep(DOWNLOAD_RETRY_SECONDS)
                continue

            response.raise_for_status()
            break
        except requests.RequestException:
            if (
                response is not None
                and response.status_code not in TRANSIENT_STATUS_CODES
            ):
                raise
            if attempt >= DOWNLOAD_RETRIES:
                raise
            print(
                f"Download failed; retrying {attempt}/{DOWNLOAD_RETRIES} "
                f"after {DOWNLOAD_RETRY_SECONDS}s: {document.url}"
            )
            sleep(DOWNLOAD_RETRY_SECONDS)

    if response is None:
        raise RuntimeError(f"Could not download {document.url}.")

    content = response.content

    if not content.startswith(b"%PDF-"):
        content_type = response.headers.get("content-type", "unknown")
        raise RuntimeError(
            f"Expected a PDF for {document.url}, got content-type {content_type}."
        )

    encoded = base64.b64encode(content).decode("ascii")
    return DownloadedDocument(
        document=document,
        filename=attachment_filename(document),
        content=content,
        content_base64=encoded,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def validate_attachment_size(
    document: DownloadedDocument,
    max_base64_bytes: int = MAX_EMAIL_BASE64_BYTES,
) -> None:
    encoded_size = len(document.content_base64)
    if encoded_size > max_base64_bytes:
        raise RuntimeError(
            f"{document.filename} is too large to attach safely "
            f"({encoded_size} base64 bytes)."
        )


def build_email_body(document: DownloadedDocument) -> tuple[str, str]:
    heading = "Novos documentos GRUL11"
    text_lines = [heading, ""]
    html_lines = [
        f"<h2>{html.escape(heading)}</h2>",
        "<ul>",
    ]

    item = document.document
    line = f"{item.date_br} | {item.doc_type} | {item.title} | {item.url}"
    text_lines.append(line)
    html_lines.append(
        "<li>"
        f"<strong>{html.escape(item.date_br)}</strong> - "
        f"{html.escape(item.doc_type)} - "
        f'<a href="{html.escape(item.url)}">'
        f"{html.escape(item.title)}</a>"
        "</li>"
    )

    html_lines.append("</ul>")
    return "\n".join(text_lines), "\n".join(html_lines)


def build_email_subject(document: Document) -> str:
    return document_mail_name(document)


def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_recipients(value: str) -> list[str]:
    recipients = [item.strip() for item in value.split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("RESEND_TO_EMAIL must contain at least one recipient.")
    return recipients


def extract_resend_id(response: Any) -> str:
    if isinstance(response, dict):
        email_id = response.get("id")
        if email_id:
            return str(email_id)
        data = response.get("data")
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])

    email_id = getattr(response, "id", None)
    if email_id:
        return str(email_id)

    raise RuntimeError(f"Resend did not return an email id: {response!r}")


def send_email(document: DownloadedDocument) -> str:
    try:
        import resend
    except ImportError as exc:
        raise RuntimeError(
            "The 'resend' package is required for sending. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc

    resend.api_key = get_required_env("RESEND_API_KEY")
    from_email = get_required_env("RESEND_FROM_EMAIL")
    to_emails = parse_recipients(get_required_env("RESEND_TO_EMAIL"))
    text, email_html = build_email_body(document)

    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": to_emails,
        "subject": build_email_subject(document.document),
        "text": text,
        "html": email_html,
        "attachments": [
            {
                "filename": document.filename,
                "content": document.content_base64,
            }
        ],
    }

    return extract_resend_id(resend.Emails.send(params))


def print_document_summary(documents: list[Document]) -> None:
    counts: dict[str, int] = {}
    for document in documents:
        counts[document.doc_type] = counts.get(document.doc_type, 0) + 1

    print(f"Found {len(documents)} unsent document(s).")
    for doc_type, count in sorted(counts.items()):
        print(f"- {doc_type}: {count}")

    for document in documents:
        print(
            f"{document.date_br} | {document.doc_type} | "
            f"{document.title} | {document.url}"
        )


def run(dry_run: bool) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    state = load_state()

    with requests.Session() as session:
        documents = fetch_documents(session)

        already_sent = sent_ids(state)
        unsent = [document for document in documents if document.id not in already_sent]

        if not unsent:
            print("No new GRUL11 documents to send.")
            return 0

        if dry_run:
            print_document_summary(unsent)
            print("Dry run only: no PDFs downloaded, no email sent, no state updated.")
            return 0

        print(f"Processing {len(unsent)} unsent document(s).")
        failures: list[tuple[Document, str]] = []

        for index, document in enumerate(unsent, start=1):
            subject = build_email_subject(document)
            print(f"Processing {index}/{len(unsent)}: {subject}")

            try:
                downloaded = download_document(session, document)
                validate_attachment_size(downloaded)
                email_id = send_email(downloaded)
            except Exception as exc:
                failures.append((document, str(exc)))
                print(f"ERROR processing {subject}: {exc}", file=sys.stderr)
                continue

            append_sent_record(state, downloaded, email_id)
            save_state(state)
            print(f"Sent email {email_id}; updated {STATE_PATH.name}.")

        if failures:
            print(
                f"Finished with {len(failures)} failed document(s). "
                f"Successful sends were saved in {STATE_PATH.name}.",
                file=sys.stderr,
            )
            for document, error in failures:
                print(
                    f"- {build_email_subject(document)} | {document.url} | {error}",
                    file=sys.stderr,
                )
            return 1

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Email new GRUL11 reports and relevant facts from Icatu Vanguarda."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List unsent documents without downloading PDFs, sending email, or updating state.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return run(dry_run=args.dry_run)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
