import csv
import os

import requests

INDEX_URL = (
    "https://ngm-store.jawafdehi.org/indices/2026-03-31/index.ciaa-press-releases.json"
)
OUTPUT_DIR = ".agents/caseworker/data"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "ciaa-press-releases.csv")

FIELDS = ["press_id", "publication_date", "title", "source_url"]
DEFAULT_TIMEOUT = 30


def fetch_all_manuscripts(index_url):
    manuscripts = []
    url = index_url
    while url:
        print(f"Fetching: {url}")
        try:
            resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(
                f"Timed out after {DEFAULT_TIMEOUT}s while fetching {url}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc

        data = resp.json()
        manuscripts.extend(data.get("manuscripts", []))
        url = data.get("next")
    return manuscripts


def save_metadata_csv(manuscripts, output_file):
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for m in manuscripts:
            meta = m.get("metadata", {})
            row = {field: meta.get(field, "") for field in FIELDS}
            writer.writerow(row)


def main():
    manuscripts = fetch_all_manuscripts(INDEX_URL)
    save_metadata_csv(manuscripts, OUTPUT_FILE)
    print(f"Saved {len(manuscripts)} records to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
