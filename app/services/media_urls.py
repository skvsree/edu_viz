"""
Media URL resolution for imported Anki cards.

Rewrites relative image URLs to use EduViz static asset URLs.
"""

import re
from urllib.parse import quote

# Pattern to match <img> tags with relative or absolute paths
IMG_PATTERN = re.compile(r'<img\s+([^>]*?)/?>', re.IGNORECASE)
SRC_PATTERN = re.compile(r'src=["\']([^"\']+)["\']')


def resolve_media_urls(html: str, deck_id: str) -> str:
    """
    Rewrite image URLs in HTML to use EduViz static asset paths.

    Anki media files are stored in app/static/media/{deck_id}/

    Args:
        html: HTML content with potential image references
        deck_id: UUID of the deck for media path

    Returns:
        HTML with resolved image URLs
    """
    def replace_src(match: re.Match) -> str:
        img_tag = match.group(0)
        src_match = SRC_PATTERN.search(img_tag)
        if not src_match:
            return img_tag

        src = src_match.group(1)

        # Skip if already absolute URL or data URI
        if src.startswith(('http://', 'https://', 'data:', '/')):
            return img_tag

        # Encode filename for safe URL
        filename = quote(src, safe='')
        new_src = f'/assets/v1/media/{deck_id}/{filename}'

        # Replace src attribute
        new_img = SRC_PATTERN.sub(f'src="{new_src}"', img_tag)
        return new_img

    return IMG_PATTERN.sub(replace_src, html)


def extract_media_filenames(html: str) -> list[str]:
    """
    Extract all media filenames referenced in HTML.

    Args:
        html: HTML content

    Returns:
        List of unique media filenames
    """
    filenames = set()
    for match in IMG_PATTERN.finditer(html):
        src_match = SRC_PATTERN.search(match.group(0))
        if src_match:
            src = src_match.group(1)
            # Skip absolute URLs and data URIs
            if not src.startswith(('http://', 'https://', 'data:', '/')):
                filenames.add(src)
    return list(filenames)


def get_media_base_path(deck_id: str) -> str:
    """
    Get the filesystem path for deck media storage.

    Args:
        deck_id: UUID of the deck

    Returns:
        Relative path from project root (e.g., 'app/static/media/{deck_id}')
    """
    return f"app/static/media/{deck_id}"


def get_media_url_prefix(deck_id: str) -> str:
    """
    Get the URL prefix for deck media assets.

    Args:
        deck_id: UUID of the deck

    Returns:
        URL prefix (e.g., '/assets/media/{deck_id}')
    """
    return f"/assets/media/{deck_id}"
