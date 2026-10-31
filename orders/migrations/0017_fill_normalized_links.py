"""Fill Order.normalized_link for existing orders, then report links ordered more than once.

The key rules are copied from orders/links.py (canonical_link) as they are
today, so this migration gives the same result if that module changes later.
Duplicates are listed by order and service ID only (never the link):
groups with more than one open order are ones the new rule would have refused.
"""

import hashlib
from collections import defaultdict
from urllib.parse import parse_qs, urlencode, urlsplit

from django.db import migrations

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


OPEN = ("accepted", "in_progress", "under_review")


def fill_normalized_links(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    groups = defaultdict(list)
    filled = 0
    for order in Order.objects.order_by("id").only("id", "service_id", "target_link", "normalized_link", "fulfillment_status"):
        key = canonical_link(order.target_link)
        if order.normalized_link != key:
            Order.objects.filter(pk=order.pk).update(normalized_link=key)
            filled += 1
        if key:
            groups[(key, order.service_id)].append((order.pk, order.fulfillment_status))
    print(f"[orders.0017] normalized_link filled for {filled} order(s).")
    duplicates = [(service_id, orders) for (key, service_id), orders in groups.items() if len(orders) > 1]
    if not duplicates:
        print("[orders.0017] No link was ordered more than once for the same service.")
    for service_id, orders in sorted(duplicates, key=lambda item: item[1][0][0]):
        listed = ", ".join(f"#{pk} ({status})" for pk, status in orders)
        open_count = sum(status in OPEN for _pk, status in orders)
        note = f" {open_count} are open at once: the new rule would have refused the later one(s)." if open_count > 1 else ""
        print(f"[orders.0017] Service #{service_id}: orders {listed} share one link.{note}")


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0016_order_normalized_link"),
    ]

    operations = [
        migrations.RunPython(fill_normalized_links, migrations.RunPython.noop),
    ]
