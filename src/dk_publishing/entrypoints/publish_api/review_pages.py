"""The HTML of the review page: plain server-rendered markup, no script, no outside files.

Everything that came from a draft (captions, names, options, reasons) goes through `escape`. The
page is written for a phone: one column, big buttons, video that plays inline.
"""

from datetime import timedelta
from html import escape

from dk_publishing.application.use_cases.daily_review import (
    DayReview,
    Decided,
    IdeaCard,
    MediaShown,
    VariantCard,
)
from dk_publishing.domain.timezones import utc_to_berlin

# Tight on purpose: no script at all, styles and forms only from this page, media over https.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src https:; media-src https:; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

STATE_LABEL = {
    "awaiting_approval": "Waiting for you",
    "invalid": "Refused by the rules",
    "scheduled": "Approved",
    "publishing": "Publishing now",
    "checking": "Being checked",
    "published": "Published",
    "failed": "Failed",
    "cancelled": "Rejected",
    "expired": "Missed its slot",
}

STYLE = """
:root{color-scheme:light dark;--bg:#faf7f2;--card:#fff;--ink:#241c15;--mute:#6b6055;--line:#e4dccf;
--ok:#1f7a3d;--okink:#fff;--no:#a32b22;--accent:#7a4b1f}
@media (prefers-color-scheme:dark){:root{--bg:#171310;--card:#221c17;--ink:#f1e9de;--mute:#a89b8c;
--line:#3a3128;--ok:#3aa862;--okink:#08170d;--no:#e0645a;--accent:#d9a066}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
main{max-width:46rem;margin:0 auto;padding:1rem 1rem 6rem}
h1{font-size:1.4rem;margin:.2rem 0}
h2{font-size:1.05rem;margin:0;word-break:break-all}
a{color:var(--accent)}
nav.days{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0 1rem}
nav.days a,nav.days button{padding:.45rem .8rem;border:1px solid var(--line);border-radius:.6rem;
background:var(--card);text-decoration:none;color:var(--ink);font:inherit}
nav.days form{display:flex;gap:.4rem;margin:0}
input[type=date]{font:inherit;padding:.4rem;border:1px solid var(--line);border-radius:.6rem;
background:var(--card);color:var(--ink)}
.idea{background:var(--card);border:1px solid var(--line);border-radius:.9rem;margin:1rem 0;
padding:.9rem}
.idea header{display:flex;flex-wrap:wrap;gap:.3rem .8rem;justify-content:space-between}
.meta{color:var(--mute);font-size:.88rem}
.media{display:grid;gap:.6rem;margin:.7rem 0}
.media video,.media img{width:100%;max-height:70vh;border-radius:.6rem;background:#000;
object-fit:contain}
.nomedia{border:1px dashed var(--line);border-radius:.6rem;padding:1rem;color:var(--mute)}
.v{border-top:1px solid var(--line);padding:.7rem 0}
.v h3{margin:0 0 .2rem;font-size:1rem;display:flex;flex-wrap:wrap;gap:.3rem .6rem;
align-items:baseline}
.chip{font-size:.78rem;padding:.1rem .5rem;border-radius:1rem;border:1px solid var(--line);
color:var(--mute)}
.chip.wait{border-color:var(--accent);color:var(--accent)}
.cap{white-space:pre-wrap;margin:.3rem 0;word-break:break-word}
dl.opt{display:grid;grid-template-columns:max-content 1fr;gap:.1rem .7rem;margin:.3rem 0;
font-size:.88rem;color:var(--mute)}
dl.opt dt{font-weight:600}dl.opt dd{margin:0;word-break:break-word}
.note{color:var(--no);font-size:.9rem;margin:.3rem 0}
.row{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.5rem}
.row form{margin:0}
button{font:inherit;font-weight:600;border-radius:.7rem;padding:.7rem 1.1rem;min-height:2.9rem;
border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.yes{background:var(--ok);color:var(--okink);border-color:var(--ok)}
button.no{color:var(--no);border-color:var(--no)}
button.big{width:100%;font-size:1.1rem;min-height:3.4rem}
.banner{border-radius:.7rem;padding:.7rem .9rem;margin:.8rem 0;border:1px solid var(--ok)}
.banner.bad{border-color:var(--no)}
.banner ul{margin:.3rem 0 0;padding-left:1.2rem}
.empty{color:var(--mute);margin:2rem 0;text-align:center}
.plain{max-width:34rem;margin:3rem auto;padding:0 1rem}
"""


def _h(text: object) -> str:
    return escape(str(text), quote=True)


def shell(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{_h(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


# --- pages that explain, with nothing on them to act on ---------------------------------------

_GET_A_LINK = (
    "Open the newest link in your morning Telegram message, or ask for a fresh one: run "
    "<code>dk review link</code> on the server."
)


def signed_out(reason: str, login: bool = False) -> str:
    """`reason`: 'none' (no session), 'expired' (a link or session that ran out), 'invalid'.
    `login`: email and password sign-in is on, so offer it first (ADR 0026)."""
    said = {
        "expired": "That link or sign-in has expired.",
        "invalid": "That link is not valid.",
    }.get(reason, "You are not signed in.")
    how = (
        '<p><a href="/review/login">Sign in with your admin email and password</a></p>'
        "<p>Or use the newest link in your morning Telegram message.</p>"
        if login
        else "<p>This page is only for the owner and opens only from a signed link. "
        f"{_GET_A_LINK}</p>"
    )
    return shell("Review: sign-in needed", f'<main class="plain"><h1>{said}</h1>{how}</main>')


def login_page(error: str = "") -> str:
    """The email and password form. One generic error for every kind of failure."""
    note = f'<p class="meta"><strong>{_h(error)}</strong></p>' if error else ""
    return shell(
        "Review: sign in",
        '<main class="plain"><h1>Sign in</h1>'
        "<p>Use the same email and password as the admin panel.</p>"
        f"{note}"
        '<form method="post" action="/review/login" style="display:grid;gap:.7rem;max-width:22rem">'
        '<input type="email" name="email" placeholder="Email" autocomplete="username" '
        'required maxlength="254" style="font:inherit;padding:.6rem">'
        '<input type="password" name="password" placeholder="Password" '
        'autocomplete="current-password" required maxlength="200" '
        'style="font:inherit;padding:.6rem">'
        '<button type="submit" style="font:inherit;padding:.7rem">Sign in</button></form></main>',
    )


def login_unavailable() -> str:
    return shell(
        "Review: sign-in unavailable",
        '<main class="plain"><h1>Sign-in is not available right now.</h1>'
        "<p>The admin login could not be reached. Nothing was changed. Try again in a minute, or "
        "use the newest link in your Telegram message.</p>"
        '<p><a href="/review/login">Try again</a></p></main>',
    )


def too_many() -> str:
    return shell(
        "Review: wait a little",
        '<main class="plain"><h1>Too many wrong attempts.</h1>'
        "<p>Nothing was changed. Wait ten minutes and open your newest link again.</p></main>",
    )


def refused(why: str) -> str:
    return shell(
        "Review: not done",
        f'<main class="plain"><h1>Nothing was changed.</h1><p>{_h(why)}</p>'
        '<p><a href="/review">Back to the review page</a></p></main>',
    )


# --- the review page ---------------------------------------------------------------------------


def _form(
    action: str, ids: tuple[str, ...], label: str, css: str, csrf: str, day: str, big: bool = False
) -> str:
    if not ids:
        return ""
    fields = "".join(f'<input type="hidden" name="v" value="{_h(i)}">' for i in ids)
    return (
        f'<form method="post" action="/review/decide">'
        f'<input type="hidden" name="csrf" value="{_h(csrf)}">'
        f'<input type="hidden" name="day" value="{_h(day)}">'
        f'<input type="hidden" name="do" value="{action}">{fields}'
        f'<button class="{css}{" big" if big else ""}">{_h(label)}</button></form>'
    )


def _when(card: VariantCard) -> str:
    return f"{utc_to_berlin(card.slot):%H:%M}"


def _media(item: MediaShown) -> str:
    used = ", ".join(p.capitalize() for p in item.platforms)
    if item.url is None:
        return (
            f'<div class="nomedia">No preview for <b>{_h(item.name)}</b> ({_h(used)}). The file '
            "was removed from the server, or this server has no preview link settings.</div>"
        )
    url = _h(item.url)
    if item.kind == "video":
        shown = f'<video controls playsinline preload="metadata" src="{url}#t=0.1"></video>'
    else:
        shown = f'<img loading="lazy" alt="{_h(item.name)}" src="{url}">'
    caption = f"{_h(item.name)} · {_h(used)}"
    return (
        f'<figure style="margin:0">{shown}<figcaption class="meta">{caption}</figcaption></figure>'
    )


def _variant(card: VariantCard, csrf: str, day: str) -> str:
    label = STATE_LABEL.get(card.state, card.state)
    waiting = card.state == "awaiting_approval"
    name = card.platform.capitalize()
    fmt = f" · {_h(card.format)}" if card.format else ""
    account = f" · {_h(card.account)}" if card.account else ""
    options = "".join(f"<dt>{_h(k)}</dt><dd>{_h(v)}</dd>" for k, v in card.options)
    files = f" · {_h(', '.join(card.media_names))}" if card.media_names else ""
    buttons = _form(
        "approve",
        (card.variant_id,) if card.can_approve else (),
        "Approve",
        "yes",
        csrf,
        day,
    ) + _form("reject", (card.variant_id,) if card.can_reject else (), "Reject", "no", csrf, day)
    return (
        f'<div class="v"><h3><span>{_h(name)}{fmt}</span>'
        f'<span class="meta">{_h(_when(card))} Berlin{account}{files}</span>'
        f'<span class="chip{" wait" if waiting else ""}">{_h(label)}</span></h3>'
        f'<p class="cap">{_h(card.caption) if card.caption else "(no caption)"}</p>'
        f"{'<dl class=opt>' + options + '</dl>' if options else ''}"
        f"{f'<p class=note>{_h(card.note)}</p>' if card.note else ''}"
        f"{f'<div class=row>{buttons}</div>' if buttons else ''}</div>"
    )


def _idea(idea: IdeaCard, csrf: str, day: str) -> str:
    media = "".join(_media(m) for m in idea.media) or (
        '<div class="nomedia">This post has no media (text only).</div>'
    )
    also = ""
    if idea.other_days:
        days = ", ".join(f"{d:%d %b}" for d in idea.other_days)
        also = f'<div class="meta">The same idea is also scheduled on {_h(days)}.</div>'
    count = len(idea.approvable)
    all_buttons = ""
    if count > 1:
        all_buttons = _form(
            "approve", idea.approvable, f"Approve this idea ({count})", "yes", csrf, day
        )
    if len(idea.rejectable) > 1:
        all_buttons += _form(
            "reject", idea.rejectable, f"Reject this idea ({len(idea.rejectable)})", "no", csrf, day
        )
    variants = "".join(_variant(v, csrf, day) for v in idea.variants)
    return (
        f'<section class="idea" id="i-{_h(idea.post_key)}"><header><h2>{_h(idea.post_key)}</h2>'
        f'<span class="meta">by agent · key {_h(idea.created_by or "unknown")}</span></header>'
        f'{also}<div class="media">{media}</div>{variants}'
        f"{f'<div class=row>{all_buttons}</div>' if all_buttons else ''}</section>"
    )


def _banner(done: Decided) -> str:
    parts = []
    if done.approved:
        parts.append(f"approved {done.approved}")
    if done.rejected:
        parts.append(f"rejected {done.rejected}")
    if done.already:
        parts.append(f"{done.already} no longer waiting, left alone")
    text = ", ".join(parts) or "nothing to do"
    problems = "".join(f"<li>{_h(p)}</li>" for p in done.problems)
    css = "banner bad" if done.problems else "banner"
    more = f"<ul>{problems}</ul>" if problems else ""
    return f'<div class="{css}" role="status"><b>Done: {_h(text)}.</b>{more}</div>'


def review_page(
    view: DayReview, today_iso: str, csrf: str, done: Decided | None = None, who: str = ""
) -> str:
    day = view.day.isoformat()
    before = (view.day - timedelta(days=1)).isoformat()
    after = (view.day + timedelta(days=1)).isoformat()
    approvable = view.approvable
    waiting = sum(1 for i in view.ideas for v in i.variants if v.state == "awaiting_approval")
    nav = (
        '<nav class="days">'
        f'<a href="/review?day={before}">&larr; Earlier</a>'
        f'<a href="/review?day={today_iso}">Today</a>'
        f'<a href="/review?day={after}">Later &rarr;</a>'
        '<form method="get" action="/review">'
        f'<input type="date" name="day" value="{day}" required><button>Go</button></form></nav>'
    )
    signed_in = (
        f'<form method="post" action="/review/logout" class="meta">Signed in as {_h(who)} '
        f'<input type="hidden" name="csrf" value="{_h(csrf)}"><button>Sign out</button></form>'
        if who
        else ""
    )
    summary = (
        f"{len(view.ideas)} idea{'s' if len(view.ideas) != 1 else ''}, {waiting} waiting for you"
    )
    everything = _form(
        "approve",
        approvable,
        f"Approve all {len(approvable)} for this day",
        "yes",
        csrf,
        day,
        big=True,
    )
    body = (
        f"<main><h1>{view.day:%A %d %B %Y}</h1>"
        f'<div class="meta">Times are Berlin time. {_h(summary)}.</div>{nav}{signed_in}'
        f"{_banner(done) if done else ''}{everything}"
        + (
            "".join(_idea(i, csrf, day) for i in view.ideas)
            or '<p class="empty">No posts from agents on this day.</p>'
        )
        + "</main>"
    )
    return shell(f"Review {day}", body)
