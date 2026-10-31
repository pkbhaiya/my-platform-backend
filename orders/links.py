"""Target links: one normalisation for every way an Order is created, and for Open Target.

Also canonical_link: the key orders on the same target are matched by.
"""

import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

INVALID_TARGET_URL = "Enter a valid target URL."
TARGET_URL_MAX_LENGTH = 2048

_validate_http_url = URLValidator(schemes=["http", "https"])
_HTTP_PREFIX = re.compile(r"^(https?)://", re.IGNORECASE)
# "javascript:", "data:", "mailto:", "ftp://"... but not "example.com:8080/x" (a host and port).
_OTHER_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)


def normalize_target_url(value):
    """The absolute https/http URL to store and open, or "" for an empty link.

    "  instagram.com/p/AbC  " -> "https://instagram.com/p/AbC"; "HTTPS://x.com" ->
    "https://x.com". Anything that is not an http(s) URL once a missing scheme is
    added (javascript:, data:, ftp:, mailto:, malformed) raises ValidationError.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError("Target link must be text.")
    text = value.strip()
    if not text:
        return ""
    match = _HTTP_PREFIX.match(text)
    if match:
        text = match.group(1).lower() + text[len(match.group(1)):]
    elif text.startswith("//"):
        text = f"https:{text}"
    elif _OTHER_SCHEME.match(text):
        raise ValidationError(INVALID_TARGET_URL)
    else:
        text = f"https://{text}"
    if len(text) > TARGET_URL_MAX_LENGTH:
        raise ValidationError("The target link is too long.")
    try:
        _validate_http_url(text)
    except ValidationError:
        raise ValidationError(INVALID_TARGET_URL)
    return text


# ---------------------------------------------------------------------------
# The duplicate-detection key (Order.normalized_link)
# ---------------------------------------------------------------------------

NORMALIZED_LINK_MAX_LENGTH = 512
_HOST_ALIASES = {"instagr.am": "instagram.com", "youtu.be": "youtube.com"}
# Instagram media: a shortcode names one post, reel or IGTV video whatever the
# prefix (instagram.com/p/<code> opens a reel too), so all four are one form.
_INSTAGRAM_MEDIA = {"p", "reel", "reels", "tv"}
# First path segments that are Instagram pages, not usernames.
_INSTAGRAM_NOT_PROFILES = {
    "accounts", "about", "api", "challenge", "developer", "direct", "emails", "explore", "legal", "oauth", "p", "reel",
    "reels", "session", "stories", "tv", "web",
}
# A profile tab is still the profile: instagram.com/<username>/reels/.
_INSTAGRAM_PROFILE_TABS = {"channel", "feed", "followers", "following", "guides", "reels", "saved", "tagged"}
# Hosts whose query string names the content; every other query parameter is dropped.
_IDENTIFYING_PARAMS = {"facebook.com": ("fbid", "id", "story_fbid", "v")}
_YOUTUBE_VIDEO_PATHS = {"embed", "live", "shorts", "v"}


def _instagram_path(segments):
    if len(segments) >= 2 and segments[0].lower() in _INSTAGRAM_MEDIA:
        return f"/p/{segments[1]}"
    # instagram.com/<username>/p/<code>/ (newer share links) is the same post.
    if len(segments) >= 3 and segments[1].lower() in _INSTAGRAM_MEDIA:
        return f"/p/{segments[2]}"
    if segments and segments[0].lower() not in _INSTAGRAM_NOT_PROFILES and (
        len(segments) == 1 or (len(segments) == 2 and segments[1].lower() in _INSTAGRAM_PROFILE_TABS)
    ):
        # Usernames are case-insensitive; shortcodes (above) are not.
        return f"/{segments[0].lower()}"
    return "/" + "/".join(segments) if segments else ""


def _youtube_video(host_was_short, segments, query):
    """The video ID of a youtube.com/watch?v=, youtu.be/, /shorts/, /embed/ or /live/ link, or None."""
    if host_was_short:
        return segments[0] if segments else None
    if segments == ["watch"]:
        return (query.get("v") or [None])[0]
    if len(segments) >= 2 and segments[0] in _YOUTUBE_VIDEO_PATHS:
        return segments[1]
    return None


def canonical_link(url):
    """The key two orders are compared by: the same target whatever way the link was written.

    https only; lower-case host without "www." / "m."; no query string or
    fragment (?igsh=, ?utm_…, #…) and no trailing slash. Instagram posts, reels,
    /reels/ and /tv/ links become instagram.com/p/<code>, and profiles
    instagram.com/<username>. YouTube and Facebook keep the one query parameter
    that names the video or post (youtube.com/watch?v=<id>), otherwise every
    video on those sites would count as the same link. "" for an empty link.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    short_youtube = host == "youtu.be"
    host = _HOST_ALIASES.get(host, host)
    segments = [segment for segment in parts.path.split("/") if segment]
    query = parse_qs(parts.query, keep_blank_values=False)

    if host == "instagram.com":
        path, kept = _instagram_path(segments), ""
    elif host == "youtube.com" and (video := _youtube_video(short_youtube, segments, query)):
        path, kept = "/watch", urlencode({"v": video})
    else:
        if host == "youtube.com" and segments and segments[0].startswith("@"):
            segments = [segments[0].lower(), *segments[1:]]  # channel handles are case-insensitive
        path = "/" + "/".join(segments) if segments else ""
        names = _IDENTIFYING_PARAMS.get(host, ())
        kept = urlencode(sorted((name, query[name][0]) for name in names if query.get(name)))
    key = f"https://{host}{path}" + (f"?{kept}" if kept else "")
    if len(key) > NORMALIZED_LINK_MAX_LENGTH:
        # Still unique and still readable at the start.
        key = f"{key[:NORMALIZED_LINK_MAX_LENGTH - 65]}#{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    return key
