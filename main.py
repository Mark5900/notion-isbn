import requests
from datetime import datetime
from dateutil import parser
import time
import schedule
from PIL import Image, ImageFilter
import re
import urllib
import logging
import boto3
from botocore.exceptions import ClientError
import os
from bs4 import BeautifulSoup
from datetime import timedelta
import textwrap
from pushbullet import Pushbullet


# Constants and Configuration
SERVICE_NAME = "Notion Books"
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
IMAGE_OVERRIDE_ID = os.getenv("NOTION_IMAGE_OVERRIDE_ID")
USE_PUSHBULLET = os.getenv("USE_PUSHBULLET", "no")  # Default to 'no' if USE_PUSHBULLET is not set
USE_AWS = os.getenv("USE_AWS", "no")  # Default to 'no' if USE_AWS is not set

GOOGLE_API_KEY = os.getenv("GoogleAPIKey")

if USE_PUSHBULLET.lower() == "yes":
    PB_TOKEN = os.getenv("PB_TOKEN")
    pb = Pushbullet(PB_TOKEN)


if USE_AWS.lower() == "yes":
    BUCKET = os.getenv("AWS_BUCKET")
    AWS_REGION = os.getenv("AWS_REGION", "us-east-2")  # Default to us-east-2 if not set
    NO_COVER_URL = f"https://{BUCKET}.s3.{AWS_REGION}.amazonaws.com/noCover.jpg"
    AWS_CLEANUP_FILES = os.getenv("AWS_CLEANUP_FILES", "no")  # Default to 'no' if not set
else:
    NO_COVER_URL = "noCover.jpg"
    AWS_CLEANUP_FILES = "no"


NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)


def cleanup_unused_s3_images():
    """Remove unused book cover and banner images from S3"""
    if USE_AWS.lower() != "yes" or AWS_CLEANUP_FILES.lower() != "yes":
        logging.info("AWS cleanup is disabled")
        return

    try:
        logging.info("Starting AWS S3 cleanup...")
        s3_client = boto3.client("s3", region_name=AWS_REGION)

        # Get all book covers and banners from S3
        s3_images = set()
        for prefix in ["book_covers/", "book_banners/"]:
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
                if 'Contents' in page:
                    for obj in page['Contents']:
                        s3_images.add(obj['Key'])

        logging.info(f"Found {len(s3_images)} images in S3")

        # Get all pages from Notion and extract cover URLs
        notion_images = set()
        pages = get_pages()

        # Safety check: if Notion returned no pages, the query likely failed.
        # Deleting now would wipe in-use images, so abort the cleanup.
        if not pages:
            logging.warning("Notion returned no pages during cleanup — skipping deletion to avoid removing in-use images")
            return

        for page in pages:
            # Check page cover (banner)
            if page.get("cover") and page["cover"].get("external"):
                cover_url = page["cover"]["external"].get("url", "")
                if BUCKET in cover_url:
                    # Extract the S3 key from the URL
                    key = cover_url.split(f"{BUCKET}.s3.{AWS_REGION}.amazonaws.com/")[-1]
                    notion_images.add(key)

            # Check Cover property (files)
            props = page.get("properties", {})
            cover_files = props.get("Cover", {}).get("files", [])
            for file in cover_files:
                if file.get("external"):
                    file_url = file["external"].get("url", "")
                    if BUCKET in file_url:
                        key = file_url.split(f"{BUCKET}.s3.{AWS_REGION}.amazonaws.com/")[-1]
                        notion_images.add(key)

        logging.info(f"Found {len(notion_images)} images used in Notion")

        # Safety check: if S3 has images but we found no references in Notion,
        # something went wrong fetching/parsing — don't delete anything.
        if s3_images and not notion_images:
            logging.warning("Found images in S3 but no references in Notion — skipping deletion as a safety measure")
            return

        # Find images in S3 that are not used in Notion
        unused_images = s3_images - notion_images

        if unused_images:
            logging.info(f"Found {len(unused_images)} unused images to delete")
            deleted_count = 0
            for key in unused_images:
                try:
                    s3_client.delete_object(Bucket=BUCKET, Key=key)
                    deleted_count += 1
                    logging.info(f"Deleted: {key}")
                except ClientError as e:
                    logging.error(f"Failed to delete {key}: {e}")

            logging.info(f"AWS cleanup completed: {deleted_count} files deleted")
        else:
            logging.info("No unused images found")

    except Exception as e:
        logging.error(f"Error during AWS cleanup: {e}")
        send_push("AWS S3 Cleanup Error", str(e))


def ensure_nocover_exists():
    """Check if noCover.jpg exists in S3, upload if it doesn't"""
    if USE_AWS.lower() != "yes":
        return

    try:
        s3_client = boto3.client("s3", region_name=AWS_REGION)
        # Try to check if noCover.jpg exists
        s3_client.head_object(Bucket=BUCKET, Key="noCover.jpg")
        logging.info("noCover.jpg already exists in S3")
    except ClientError as e:
        # If 404, file doesn't exist - upload it
        if e.response['Error']['Code'] == '404':
            logging.info("noCover.jpg not found in S3, uploading...")
            try:
                s3_client.upload_file("noCover.jpg", BUCKET, "noCover.jpg")
                logging.info(f"Successfully uploaded noCover.jpg to {NO_COVER_URL}")
            except Exception as upload_error:
                logging.error(f"Failed to upload noCover.jpg: {upload_error}")
        else:
            logging.error(f"Error checking noCover.jpg in S3: {e}")


def send_push(subject, message):
    if os.getenv("USE_PUSHBULLET", "").lower() == "yes":
        push = pb.push_note(subject, message)


def remove_html(input_string):
    soup = BeautifulSoup(input_string, "html.parser")
    return soup.get_text()


def upload_file(file_name, object_name, bucket_folder):
    s3_client = boto3.client("s3", region_name=AWS_REGION)
    object_name = f"{bucket_folder}{object_name or os.path.basename(file_name)}"

    try:
        s3_client.upload_file(file_name, BUCKET, object_name)
    except ClientError as e:
        logging.error(e)
        return False
    return True


# Updated get_book function to use both Google Books API and OpenLibrary API
def get_book(isbn):
    isbn = str(isbn)
    # Try Google Books API first
    base_url_google = f"https://www.googleapis.com/books/v1/volumes?country=US&q=isbn:{isbn}&key={GOOGLE_API_KEY}"
    try:
        response = requests.get(base_url_google)
        response.raise_for_status()
        book_data = response.json()

        if book_data["totalItems"] > 0:
            book_info = book_data["items"][0]["volumeInfo"]
            google_title = book_info.get('title', '').strip()
            # Skip placeholder/empty Google titles (e.g. "Untitled Project 1") and fall back to OpenLibrary
            if google_title and not google_title.lower().startswith("untitled"):
                logging.info(f"Found {google_title} using Google Books API")
                # Standardize the data
                standardized_data = {
                    'title': book_info.get('title', ''),
                    'subtitle': book_info.get('subtitle', ''),
                    'authors': book_info.get('authors', []),
                    'published_date': book_info.get('publishedDate', ''),
                    'description': book_info.get('description', ''),
                    'publisher': book_info.get('publisher', ''),
                    'page_count': book_info.get('pageCount', 0),
                    'cover_url': None,
                }
                # Get cover image URL
                if 'imageLinks' in book_info and 'thumbnail' in book_info['imageLinks']:
                    standardized_data['cover_url'] = book_info['imageLinks']['thumbnail']
                else:
                    standardized_data['cover_url'] = None
                return standardized_data
            else:
                logging.info(f"Google Books returned an unusable title ('{google_title}') for ISBN {isbn}, trying OpenLibrary API")
        else:
            logging.info("Book not found in Google Books API, trying OpenLibrary API")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error accessing Google Books API: {e}")
        send_push(f"Error attempting to find book {isbn} in Google Books API", str(e))

    # Try OpenLibrary API
    base_url_openlibrary = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=details&format=json"
    try:
        response = requests.get(base_url_openlibrary)
        response.raise_for_status()
        book_data = response.json()
        if f"ISBN:{isbn}" in book_data:
            book_info = book_data[f"ISBN:{isbn}"]
            details = book_info.get("details", {})
            logging.info(f"Found {details.get('title', 'Unknown Title')} using OpenLibrary API")
            # Standardize the data
            standardized_data = {
                'title': details.get('title', ''),
                'subtitle': details.get('subtitle', ''),
                'authors': [],
                'published_date': details.get('publish_date', ''),
                'description': '',
                'publisher': ', '.join(details.get('publishers', [])) if 'publishers' in details else '',
                'page_count': details.get('number_of_pages', 0),
                'cover_url': None,
            }
            # Authors
            if 'authors' in details:
                standardized_data['authors'] = [author.get('name', 'Unknown') for author in details['authors']]
            else:
                standardized_data['authors'] = []

            # Description
            if 'description' in details:
                if isinstance(details['description'], dict):
                    standardized_data['description'] = details['description'].get('value', '')
                elif isinstance(details['description'], str):
                    standardized_data['description'] = details['description']
            else:
                standardized_data['description'] = ''

            # Cover URL
            if 'covers' in details and details['covers']:
                cover_id = details['covers'][0]
                standardized_data['cover_url'] = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
            elif 'thumbnail_url' in book_info:
                standardized_data['cover_url'] = book_info['thumbnail_url'].replace('-S.jpg', '-L.jpg')
            else:
                standardized_data['cover_url'] = None

            return standardized_data
        else:
            logging.info("Book not found in OpenLibrary API")
            logging.warning(
                "No data found for the following ISBN %s. Check and see if it is correct",
                isbn,
            )
            send_push(
                f"No data found for ISBN: {isbn}",
                f"Check for another ISBN. No data was found for ISBN: {isbn}",
            )
            return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error accessing OpenLibrary API: {e}")
        send_push(f"Error attempting to find book {isbn} in OpenLibrary API", str(e))
        return None


def get_pages(num_pages=None):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload = {"page_size": 100 if num_pages is None else num_pages}
    logging.info("Looking for New Books...")
    results = []

    try:
        with requests.post(url, json=payload, headers=NOTION_HEADERS) as response:
            logging.info(f"API Response Status: {response.status_code}")
            data = response.json()

            # Log any errors from Notion API
            if "error" in data:
                logging.error(f"Notion API Error: {data['error']}")
                logging.error(f"Error message: {data.get('message', 'No message')}")
                return results

            results.extend(data["results"])
            logging.info(f"First batch: Retrieved {len(data['results'])} pages")

            while data.get("has_more") and num_pages is None:
                payload["start_cursor"] = data["next_cursor"]
                with requests.post(url, json=payload, headers=NOTION_HEADERS) as response:
                    data = response.json()
                    results.extend(data["results"])
                    logging.info(f"Next batch: Retrieved {len(data['results'])} more pages")
    except Exception as e:
        logging.error(f"Error in get_pages: {e}")
        import traceback
        logging.error(traceback.format_exc())

    return results


def read_pages():
    pages = get_pages()
    logging.info(f"Retrieved {len(pages)} total pages from database")

    for count, page in enumerate(pages, start=1):
        try:
            page_id = page["id"]
            props = page["properties"]

            # Safely retrieve ISBN
            isbn_field = props.get("ISBN", {}).get("rich_text", [])
            if isbn_field:
                isbn = isbn_field[0].get("plain_text", "")
            else:
                logging.error(f"No ISBN found for page {page_id}. Skipping...")
                continue  # or handle accordingly

            title_field = props.get("Name", {}).get("title", [])
            if title_field:
                title = title_field[0].get("plain_text", "")
            else:
                logging.error(f"No title found for page {page_id}. Skipping...")
                continue

            if title.startswith("New ") and title.endswith(" Book") and isbn:
                logging.info(f"Found a new book: '{title}' with ISBN: {isbn}")
                book_data = get_book(isbn)

                if book_data:
                    update_notion(book_data, page_id, isbn)
        except (KeyError, IndexError) as e:
            logging.error(f"Error reading page {count}: {e}")
            send_push("Error reading Notion book page", f"Page {count}: {e}")


def update_page(page_id, data):
    url = f"https://api.notion.com/v1/pages/{page_id}"

    res = requests.patch(url, json=data, headers=NOTION_HEADERS)

    if res.status_code == 200:
        logging.info(f"Book details updated successfully for page {page_id}!")
        response_data = res.json()
        # Log the actual cover URL that was set
        if response_data.get("cover") and response_data["cover"].get("external"):
            logging.info(f"Notion confirmed cover URL: {response_data['cover']['external']['url']}")
    else:
        logging.error(
            f"Notion update request failed with status code: {res.status_code}"
        )
        json_data = res.json()
        for key, value in json_data.items():
            logging.error(f"{key}: {value}")
        send_push(
            f"Error {json_data['status']}: {json_data['code']}", json_data["message"]
        )


def make_banner(img_url, page_id):
    img_name = f"{page_id}.jpg"
    urllib.request.urlretrieve(img_url, img_name)

    img = Image.open(img_name).convert("RGB")
    new_height = 540
    new_width = int(new_height * img.width / img.height)
    img_poster = img.resize((new_width, new_height))
    upload_file(img_name, img_name, "book_covers/")

    cropped_img = (
        img.crop((5, img.height // 3, img.width, 2 * img.height // 3))
        .resize((1500, 600))
        .filter(ImageFilter.BoxBlur(30))
    )
    cropped_img.paste(img_poster, (573, 30))
    cropped_img.save(img_name)

    upload_file(img_name, img_name, "book_banners/")

    return cropped_img


def get_book_cover_from_isbndb(isbn):
    # Send a request to the ISBNdb book page
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
    book_url = (f"https://isbndb.com/book/{isbn}")
    response = requests.get(book_url, headers=headers)

    # Parse the page content
    soup = BeautifulSoup(response.content, 'html.parser')

    # Find the div with the class "artwork" and then locate the object tag inside it
    artwork_div = soup.find("div", {"class": "artwork"})

    if artwork_div:
        # Find the object tag within the div and get the data attribute
        object_tag = artwork_div.find("object")
        if object_tag and "data" in object_tag.attrs:
            image_url = object_tag['data']
            logging.info(f"Cover image URL: {image_url}")
            return image_url
        else:
            logging.info("Object tag or 'data' attribute not found.")
    else:
        logging.info("Div with class 'artwork' not found.")


def get_book_cover_from_openlibrary(isbn):
    url = (
        f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json"
    )

    try:
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            book_info = response.json().get(f"ISBN:{isbn}", {})
            openlibrary_ids = book_info.get("identifiers", {}).get("openlibrary", [])

            if openlibrary_ids:
                return f"https://covers.openlibrary.org/b/olid/{openlibrary_ids[0]}-L.jpg"
            else:
                logging.info("No openlibrary ID found.")
        else:
            logging.info(f"Failed to retrieve data. Status code: {response.status_code}")
    except requests.exceptions.Timeout:
        logging.warning(f"Timeout connecting to OpenLibrary for ISBN {isbn}")
    except requests.exceptions.RequestException as e:
        logging.warning(f"Error connecting to OpenLibrary for ISBN {isbn}: {e}")


def get_image_override_pages():
    """Get all pages from the image override database that are not yet processed"""
    if not IMAGE_OVERRIDE_ID:
        logging.info("IMAGE_OVERRIDE_ID not configured, skipping override check")
        return []

    url = f"https://api.notion.com/v1/databases/{IMAGE_OVERRIDE_ID}/query"
    payload = {
        "page_size": 100,
        "filter": {
            "property": "Status",
            "checkbox": {
                "equals": False
            }
        }
    }

    logging.info("Checking for image overrides...")
    results = []

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=NOTION_HEADERS)
            if response.status_code == 404:
                if attempt < 2:
                    logging.warning(f"Image override database returned 404, retrying in 5s (attempt {attempt + 1}/3)...")
                    time.sleep(5)
                    continue
                logging.warning("Image override database not found (404). Check that IMAGE_OVERRIDE_ID is correct and the Notion integration has access to it.")
                return results
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logging.error(f"Notion API Error: {data['error']}")
                return results

            results.extend(data["results"])
            logging.info(f"Found {len(results)} image override entries to process")

            while data.get("has_more"):
                payload["start_cursor"] = data["next_cursor"]
                response = requests.post(url, json=payload, headers=NOTION_HEADERS)
                data = response.json()
                results.extend(data["results"])

            return results

        except Exception as e:
            if attempt < 2:
                logging.warning(f"Error getting image overrides (attempt {attempt + 1}/3): {e}, retrying in 5s...")
                time.sleep(5)
            else:
                logging.error(f"Error getting image overrides after 3 attempts: {e}")

    return results


def find_book_by_isbn(isbn):
    """Find a book page in the main database by ISBN"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload = {
        "filter": {
            "property": "ISBN",
            "rich_text": {
                "equals": str(isbn)
            }
        }
    }

    try:
        response = requests.post(url, json=payload, headers=NOTION_HEADERS)
        response.raise_for_status()
        data = response.json()

        if data.get("results"):
            return data["results"][0]
        else:
            logging.warning(f"No book found with ISBN: {isbn}")
            return None

    except Exception as e:
        logging.error(f"Error finding book by ISBN {isbn}: {e}")
        return None


def update_override_status(page_id, status=True):
    """Mark an image override entry as processed"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {
        "properties": {
            "Status": {"checkbox": status}
        }
    }

    try:
        response = requests.patch(url, json=payload, headers=NOTION_HEADERS)
        response.raise_for_status()
        logging.info(f"Override entry {page_id} marked as processed")
    except Exception as e:
        logging.error(f"Error updating override status: {e}")


def process_image_overrides():
    """Process all pending image override entries"""
    if USE_AWS.lower() != "yes":
        logging.info("AWS not enabled, skipping image overrides")
        return

    override_pages = get_image_override_pages()

    for override_page in override_pages:
        try:
            props = override_page.get("properties", {})

            # Extract ISBN
            isbn_prop = props.get("ISBN", {}).get("rich_text", [])
            if not isbn_prop:
                logging.warning(f"Override entry {override_page['id']} missing ISBN")
                continue
            isbn = isbn_prop[0].get("plain_text", "").strip()

            # Extract Link - try multiple possible property names and types
            link_prop = None

            # Try as title property (Name column with embedded link)
            if props.get("Link", {}).get("title", []):
                title_content = props.get("Link", {}).get("title", [])
                if title_content and title_content[0].get("href"):
                    link_prop = title_content[0].get("href")
            # Try as URL property
            elif props.get("Link", {}).get("url"):
                link_prop = props.get("Link", {}).get("url")
            # Try Name (title) property which might contain a URL
            elif props.get("Name", {}).get("title", []):
                title_content = props.get("Name", {}).get("title", [])
                if title_content and title_content[0].get("href"):
                    link_prop = title_content[0].get("href")
            # Try as rich_text property
            elif props.get("Link", {}).get("rich_text", []):
                rich_text = props.get("Link", {}).get("rich_text", [])
                if rich_text:
                    link_prop = rich_text[0].get("plain_text", "").strip()

            if not link_prop:
                logging.warning(f"Override entry {override_page['id']} missing Link URL")
                continue

            # Find the book in the main database
            book_page = find_book_by_isbn(isbn)
            if not book_page:
                logging.error(f"Book with ISBN {isbn} not found in database")
                send_push("Image Override Error", f"Book with ISBN {isbn} not found")
                continue

            book_page_id = book_page["id"]
            book_title = book_page.get("properties", {}).get("Name", {}).get("title", [{}])[0].get("plain_text", "Unknown")

            # Download the image
            img_name = f"{book_page_id}.jpg"
            try:
                logging.info(f"Downloading custom image from: {link_prop}")
                urllib.request.urlretrieve(link_prop, img_name)
                img = Image.open(img_name).convert("RGB")

                # Create banner and upload both to AWS
                make_banner(link_prop, book_page_id)

                banner_url = f"https://{BUCKET}.s3.{AWS_REGION}.amazonaws.com/book_banners/{book_page_id}.jpg"
                cover_url = f"https://{BUCKET}.s3.{AWS_REGION}.amazonaws.com/book_covers/{book_page_id}.jpg"

                logging.info(f"Generated URLs - Banner: {banner_url}")
                logging.info(f"Generated URLs - Cover: {cover_url}")

                # Update the book with the new images
                update_data = {
                    "cover": {"external": {"url": banner_url}},
                    "properties": {
                        "Cover": {"files": [{"name": book_title, "external": {"url": cover_url}}]}
                    }
                }

                logging.info(f"Sending update to Notion for page {book_page_id}")
                logging.info(f"Update data: {update_data}")
                update_page(book_page_id, update_data)

                # Mark the override entry as processed
                update_override_status(override_page["id"], True)

                logging.info(f"Successfully updated book '{book_title}' with custom image")
                send_push("Custom Book Cover Applied", f"Updated '{book_title}' (ISBN: {isbn}) with custom image")

                # Clean up
                os.remove(img_name)

            except Exception as e:
                logging.error(f"Error processing image for ISBN {isbn}: {e}")
                send_push("Image Override Error", f"Failed to process image for ISBN {isbn}: {str(e)}")
                if os.path.exists(img_name):
                    os.remove(img_name)

        except Exception as e:
            logging.error(f"Error processing override entry: {e}")
            import traceback
            logging.error(traceback.format_exc())


def update_notion(book_data, page_id, isbn):
    title = book_data.get("title", "").strip()

    subtitle = book_data.get("subtitle", "").strip()
    if subtitle:
        # Strip a trailing colon on the title so we don't end up with "Title:: Subtitle"
        title = title.rstrip(": ").strip()
        title += f": {subtitle}"
    title = re.sub(r"\([^)]*\)", "", title)[:100]

    # Get cover URL with fallbacks
    cover = book_data.get('cover_url')
    if not cover:
        cover = get_book_cover_from_openlibrary(isbn)
    if not cover:
        cover = get_book_cover_from_isbndb(isbn)

    img_name = f"{page_id}.jpg"
    use_default_cover = False

    if cover:
        try:
            urllib.request.urlretrieve(cover, img_name)
            img = Image.open(img_name)
        except Exception as e:
            logging.warning(f"Could not download cover from {cover}: {e}")
            use_default_cover = True
    else:
        use_default_cover = True

    if use_default_cover:
        # Use local noCover.jpg file
        import shutil
        shutil.copy("noCover.jpg", img_name)
        img = Image.open(img_name)
        cover = None  # Will be handled later for AWS upload

    if USE_AWS.lower() == "no":
        if use_default_cover:
            # Use local noCover.jpg file
            cover = "noCover.jpg"
            banner = cover
        else:
            cover = cover
            banner = cover
    elif img.size < (50, 50) or use_default_cover:
        # Use the central noCover.jpg from S3 instead of uploading per-book copies
        cover = NO_COVER_URL
        banner = NO_COVER_URL
    else:
        make_banner(cover, page_id)
        banner = (
            f"https://{BUCKET}.s3.{AWS_REGION}.amazonaws.com/book_banners/{page_id}.jpg"
        )
        cover = f"https://{BUCKET}.s3.{AWS_REGION}.amazonaws.com/book_covers/{page_id}.jpg"

    authors = book_data.get("authors", ["Anthology"])
    # Ensure authors is not empty
    if not authors or (isinstance(authors, list) and len(authors) == 0):
        authors = ["Unknown Author"]

    published_date = book_data.get("published_date", "")
    description = remove_html(book_data.get("description", ""))
    description = textwrap.shorten(
        description.replace('"', "").replace("\n", ""), width=2000, placeholder="..."
    )
    publisher = (
        book_data.get("publisher", "No Publisher Found")
        .replace(",", "")
        .replace(";", "")
    )
    # Ensure publisher is not empty
    if not publisher or publisher.strip() == "":
        publisher = "Unknown Publisher"

    year = parser.parse(published_date).year if published_date else None
    page_count = book_data.get("page_count", 0)

    update_data = {
        # Notion only accepts cover URLs over HTTPS
        "cover": {"external": {"url": banner.replace("http://", "https://")}},
        "properties": {
            "Author": {"multi_select": [{"name": author} for author in authors]},
            "Publisher": {"select": {"name": publisher}},
            "ISBN": {"rich_text": [{"text": {"content": isbn}}]},
            "Summary": {"rich_text": [{"text": {"content": description}}]},
            "Type": {"select": {"name": "Physical"}},
            "Cover": {"files": [{"name": title, "external": {"url": cover}}]},
            "Year": {"number": year},
            "Pages": {"number": page_count},
            "Name": {"title": [{"text": {"content": title}}]},
        },
    }

    update_page(page_id, update_data)
    send_push("New book found!!!", f"Adding {title} to your book collection")
    os.remove(img_name)


# Ensure noCover.jpg exists in S3 before starting
ensure_nocover_exists()

# Run cleanup once at startup if enabled
if AWS_CLEANUP_FILES.lower() == "yes":
    cleanup_unused_s3_images()
    # Schedule daily cleanup at startup time
    schedule.every().day.do(cleanup_unused_s3_images)
    logging.info("AWS cleanup scheduled to run daily")

# Process image overrides at startup
process_image_overrides()

# Schedule regular tasks
read_pages()
schedule.every(60).seconds.do(read_pages)
schedule.every(60).seconds.do(process_image_overrides)
logging.info("Next scan scheduled...")

while True:
    schedule.run_pending()
    time.sleep(1)
