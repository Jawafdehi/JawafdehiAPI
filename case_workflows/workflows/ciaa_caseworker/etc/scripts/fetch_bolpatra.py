"""
Fetch bolpatra (procurement documents) from bolpatra.gov.np

This script searches for procurement documents using an IFB/RFP/EOI/PQ number
and downloads all associated PDFs (bid documents, addendums, etc.)
"""

import os
import re
import sys
import tempfile
import time

import requests
import urllib3
from bs4 import BeautifulSoup

# Request timeout in seconds (connect, read)
DEFAULT_TIMEOUT = (10, 60)

# TLS verification is enabled by default.
# Use INSECURE_SKIP_SSL=1 only when endpoints are known to be misconfigured.
INSECURE_SKIP_SSL = os.environ.get("INSECURE_SKIP_SSL", "").lower() in (
    "1",
    "true",
    "yes",
)

if INSECURE_SKIP_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print(
        "   WARNING: TLS certificate verification disabled via INSECURE_SKIP_SSL",
        file=sys.stderr,
    )
    print(
        "   Use this only for temporary troubleshooting on trusted networks.\n",
        file=sys.stderr,
    )


class BolpatraFetcher:
    BASE_URL = "https://www.bolpatra.gov.np/egp"

    def __init__(self, output_dir=None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"{self.BASE_URL}/searchOpportunity",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

        # Keep TLS verification on by default; allow explicit insecure override.
        self.session.verify = not INSECURE_SKIP_SSL

        # Default output directory relative to script location
        if output_dir is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            output_dir = os.path.join(script_dir, "data", "bolpatra")

        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def search_by_ifb_number(self, ifb_number):
        """Search for tenders by IFB/RFP/EOI/PQ number"""
        print(f"Searching for IFB number: {ifb_number}")

        params = {
            "bidSearchTO.title": "",
            "bidSearchTO.ifbNO": ifb_number,
            "bidSearchTO.procurementCategory": "-1",
            "bidSearchTO.procurementMethod": "-1",
            "bidSearchTO.publicEntityTitle": "",
            "bidSearchTO.publicEntity": "0",
            "parentPE": "0",
            "null_widget": "",
            "bidSearchTO.childPEId": "-1",
            "bidSearchTO.NoticePubDateText": "",
            "bidSearchTO.lastBidSubmissionDateText": "",
            "bidSearchTO.contractType": "-1",
            "currentPageIndexInput": "1",
            "pageSizeInput": "10",
            "pageActionInput": "first",
            "tenderId": "",
            "addNewJV": "false",
            "currentPageIndex": "1",
            "pageSize": "10",
            "pageAction": "",
            "totalRecords": "1",
            "startIndex": "0",
            "numberOfPages": "1",
            "isNextAvailable": "false",
            "isPreviousAvailable": "false",
            "emergencyFlag": "0",
            "_": str(int(time.time() * 1000)),
        }

        url = f"{self.BASE_URL}/SearchOpportunitiesResultHomePage"
        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()

        return self._parse_search_results(response.text)

    def _parse_search_results(self, html):
        """Parse search results to extract tender IDs and basic info"""
        soup = BeautifulSoup(html, "html.parser")
        results = []

        table = soup.find("table", {"id": "dashBoardBidResult"})
        if not table:
            print("No results table found")
            return results

        tbody = table.find("tbody")
        if not tbody:
            # Try to find rows directly if no tbody
            rows = table.find_all("tr", class_=re.compile(r"reltab"))
        else:
            rows = tbody.find_all("tr")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            # Extract tender ID from onclick attribute
            link = cells[2].find("a")
            if link and "onclick" in link.attrs:
                onclick = link["onclick"]
                match = re.search(r"getTenderDetails\('(\d+)'\)", onclick)
                if match:
                    tender_id = match.group(1)
                    ifb_text = cells[1].get_text(strip=True).split("\n")[0].strip()
                    result = {
                        "tender_id": tender_id,
                        "ifb_number": ifb_text or "Unknown",
                        "project_title": link.get_text(strip=True),
                        "public_entity": cells[3].get_text(strip=True),
                        "procurement_type": cells[4].get_text(strip=True),
                        "status": cells[5].get_text(strip=True),
                    }
                    results.append(result)
                    print(
                        f"Found tender: {result['ifb_number']} - {result['project_title']}"
                    )

        return results

    def get_tender_details(self, tender_id):
        """Get detailed information about a tender"""
        print(f"Fetching tender details for ID: {tender_id}")

        url = f"{self.BASE_URL}/getTenderDetails"
        params = {"tenderId": tender_id, "_": str(int(time.time() * 1000))}

        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()

        return self._parse_tender_details(response.text, tender_id)

    def _parse_tender_details(self, html, tender_id):
        """Parse tender details page to extract document download links"""
        soup = BeautifulSoup(html, "html.parser")

        details = {"tender_id": tender_id, "documents": []}

        # Extract basic info by finding rows with IFB/RFP/EOI/PQ label
        rows = soup.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 2:
                label_text = cells[0].get_text(strip=True)
                if "IFB/RFP/EOI/PQ" in label_text:
                    details["ifb_number"] = cells[1].get_text(strip=True)
                elif "Public Entity" in label_text or "Organization" in label_text:
                    details["public_entity"] = cells[1].get_text(strip=True)

        # Find all download links
        download_links = soup.find_all("a", href=re.compile(r"download\?"))

        for link in download_links:
            href = link["href"]
            # Extract alfId and docId from href
            match = re.search(r"alfId=([^&]+)&docId=(\d+)", href)
            if match:
                alf_id = match.group(1)
                doc_id = match.group(2)

                # Find document type and date from parent table
                row = link.find_parent("tr")
                if row:
                    cells = row.find_all("td")
                    doc_type = (
                        cells[0].get_text(strip=True) if len(cells) > 0 else "Unknown"
                    )
                    pub_date = (
                        cells[1].get_text(strip=True) if len(cells) > 1 else "Unknown"
                    )

                    details["documents"].append(
                        {
                            "type": doc_type,
                            "publication_date": pub_date,
                            "alf_id": alf_id,
                            "doc_id": doc_id,
                            "download_url": f"{self.BASE_URL}/{href.lstrip('/')}",
                        }
                    )
                    print(f"  Found document: {doc_type} ({pub_date})")

        return details

    def download_document(self, doc_info, tender_id, ifb_number):
        """Download a single document"""
        url = doc_info["download_url"]

        # Sanitize filename components - replace any non-safe characters
        doc_type = re.sub(r"[^A-Za-z0-9._-]+", "_", doc_info["type"])
        pub_date = re.sub(r"[^A-Za-z0-9._-]+", "_", doc_info["publication_date"])
        doc_id = doc_info["doc_id"]

        # Create filename with unique identifiers to prevent overwrites
        safe_ifb = re.sub(r"[^A-Za-z0-9._-]+", "-", ifb_number)
        safe_tender_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(tender_id))
        safe_doc_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(doc_id))
        filename = (
            f"{safe_ifb}_{safe_tender_id}_{safe_doc_id}_{doc_type}_{pub_date}.pdf"
        )
        filepath = os.path.join(self.output_dir, filename)

        print(f"Downloading: {filename}")

        try:
            with self.session.get(
                url, stream=True, timeout=DEFAULT_TIMEOUT
            ) as response:
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and not (
                    "application/pdf" in content_type
                    or "application/octet-stream" in content_type
                ):
                    print(
                        f"  Unexpected content type for {url}: {content_type}; skipping"
                    )
                    return None

                iterator = response.iter_content(chunk_size=8192)
                first_chunk = b""
                for chunk in iterator:
                    if chunk:
                        first_chunk = chunk
                        break

                if not first_chunk:
                    print(f"  Empty response body for {url}; skipping")
                    return None

                if len(first_chunk) < 4 or not first_chunk.startswith(b"%PDF"):
                    print(
                        f"  Response for {url} is not a valid PDF (missing %PDF signature); skipping"
                    )
                    return None

                # Write atomically: stream to a temp file in the same directory,
                # then os.replace() to final path so a partial download is never
                # left at `filepath` if the process is interrupted mid-write.
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.output_dir, suffix=".tmp", delete=False
                    ) as tmp_f:
                        tmp_path = tmp_f.name
                        tmp_f.write(first_chunk)
                        for chunk in iterator:
                            if chunk:
                                tmp_f.write(chunk)
                    os.replace(tmp_path, filepath)
                    tmp_path = None  # replaced successfully; nothing to clean up
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass

            print(f"  Saved to: {filepath}")
            return filepath
        except requests.exceptions.RequestException as e:
            print(f"  Error downloading from {url}: {e}")
            return None
        except OSError as e:
            print(f"  File I/O error writing to {filepath}: {e}")
            return None

    def fetch_all_documents(self, ifb_number):
        """Main method to search and download all documents for an IFB number

        Returns:
            dict: {'downloaded': [filepaths], 'failed': [error_dicts]}
        """
        print(f"\n{'='*60}")
        print(f"Fetching bolpatra documents for: {ifb_number}")
        print(f"{'='*60}\n")

        results = {"downloaded": [], "failed": []}

        # Search for the tender
        try:
            search_results = self.search_by_ifb_number(ifb_number)
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"Error searching for tender: {e}")
            results["failed"].append({"stage": "search", "error": str(e)})
            return results
        except Exception as e:
            print(f"Unexpected error searching for tender: {e}")
            results["failed"].append({"stage": "search", "error": f"Unexpected: {e!s}"})
            return results

        if not search_results:
            print("No tenders found")
            return results

        # Process each result
        for result in search_results:
            tender_id = result["tender_id"]
            print(f"\nProcessing tender ID: {tender_id}")

            # Get tender details
            try:
                details = self.get_tender_details(tender_id)
            except (requests.exceptions.RequestException, ValueError) as e:
                print(f"Error fetching tender details: {e}")
                results["failed"].append(
                    {"stage": "details", "tender_id": tender_id, "error": str(e)}
                )
                continue
            except Exception as e:
                print(f"Unexpected error fetching tender details: {e}")
                results["failed"].append(
                    {
                        "stage": "details",
                        "tender_id": tender_id,
                        "error": f"Unexpected: {e!s}",
                    }
                )
                continue

            # Download all documents
            print(f"\nDownloading {len(details['documents'])} documents...")
            for doc in details["documents"]:
                filepath = self.download_document(doc, tender_id, result["ifb_number"])
                if filepath:
                    results["downloaded"].append(filepath)
                else:
                    results["failed"].append(
                        {
                            "stage": "download",
                            "tender_id": tender_id,
                            "doc_type": doc.get("type", "Unknown"),
                            "url": doc.get("download_url", "Unknown"),
                        }
                    )

        print(f"\n{'='*60}")
        print(
            f"Downloaded {len(results['downloaded'])} documents to: {self.output_dir}"
        )
        if results["failed"]:
            print(f"Failed: {len(results['failed'])} operations")
        print(f"{'='*60}\n")

        return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_bolpatra.py <IFB_NUMBER>")
        print("Example: python fetch_bolpatra.py 'Re-PPHL2/G/NCB/02/2079-80'")
        sys.exit(1)

    ifb_number = sys.argv[1]

    fetcher = BolpatraFetcher()
    results = fetcher.fetch_all_documents(ifb_number)

    if results["downloaded"]:
        print("\nDownloaded files:")
        for f in results["downloaded"]:
            print(f"  ✓ {f}")

    if results["failed"]:
        print("\nFailed operations:")
        for failure in results["failed"]:
            print(f"  ✗ {failure}")

    # Exit with error code if nothing was downloaded or if any operations failed
    if not results["downloaded"]:
        print("\nNo files downloaded")
        sys.exit(1)
    elif results["failed"]:
        print(
            f"\nDownloaded {len(results['downloaded'])} file(s), but {len(results['failed'])} operation(s) failed",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print(f"\nSuccessfully downloaded {len(results['downloaded'])} file(s)")
        sys.exit(0)


if __name__ == "__main__":
    main()
