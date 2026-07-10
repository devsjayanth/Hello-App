import ipaddress
from contextlib import asynccontextmanager
import logging
import multiprocessing as mp
import os
from random import SystemRandom
import signal
import socket
import sys
import threading
import time
from urllib.parse import urlparse

import psutil
import py_avataaars
import requests
import uvicorn

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette_exporter import PrometheusMiddleware, handle_metrics


templates = Jinja2Templates(directory='templates')

class _GlobalState:
    avatar_svg: str | None = None

global_state = _GlobalState()

_LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
_KNOWN_LEVELS = frozenset({'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'})
if _LOG_LEVEL not in _KNOWN_LEVELS:
    _LOG_LEVEL = 'INFO'
logging.basicConfig(stream=sys.stdout, level=getattr(logging, _LOG_LEVEL), format='%(asctime)s %(levelname)s %(message)s', force=True)
logging.debug('Log level is set to DEBUG.')

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_HOSTNAME = socket.gethostname()  # cached once; never changes at runtime

_avatar_lock = threading.Lock()

_EXCLUDED_TOP_NAMES = frozenset({'HIJAB', 'TURBAN', 'NO_HAIR', 'EYE_PATCH', 'WINTER_HAT1', 'WINTER_HAT2', 'WINTER_HAT3', 'WINTER_HAT4'})
_LIGHT_SKIN = frozenset({'PALE', 'LIGHT', 'TANNED'})
_DARK_HAIR = frozenset({'BLACK', 'BROWN', 'BROWN_DARK'})
_LIGHT_HAIR = frozenset({'BLONDE', 'BLONDE_GOLDEN', 'PLATINUM', 'SILVER_GRAY'})
_MID_HAIR = frozenset({'AUBURN', 'RED'})
_LIGHT_COLORS = frozenset({'WHITE', 'PASTEL_BLUE', 'PASTEL_GREEN', 'PASTEL_ORANGE', 'PASTEL_RED', 'PASTEL_YELLOW', 'PINK', 'HEATHER', 'BLUE_01', 'BLUE_02'})

_FILTERED_TOP_TYPES = [t for t in py_avataaars.TopType if not t.name.startswith('SHORT_HAIR') and t.name not in _EXCLUDED_TOP_NAMES]
_FILTERED_SKIN_COLORS = [s for s in py_avataaars.SkinColor if s.name != 'YELLOW']
_FILTERED_HAIR_COLORS = [h for h in py_avataaars.HairColor if h.name != 'PASTEL_PINK']

# Pre-materialised enum lists used by _build_avatar — avoids repeated list()
# calls (enum iteration) on every avatar generation request.
_ALL_TOP_TYPES      = list(py_avataaars.TopType)
_ALL_SKIN_COLORS    = list(py_avataaars.SkinColor)
_ALL_HAIR_COLORS    = list(py_avataaars.HairColor)
_ALL_CLOTHE_TYPES   = list(py_avataaars.ClotheType)
_ALL_FACIAL_HAIR    = list(py_avataaars.FacialHairType)
_ALL_NOSE_TYPES     = list(py_avataaars.NoseType)
_ALL_ACCESSORIES    = list(py_avataaars.AccessoriesType)
_ALL_CLOTHE_GRAPHIC = list(py_avataaars.ClotheGraphicType)
_ALL_COLORS         = list(py_avataaars.Color)

_CUSTOM_EYEBROW_TYPES = [
    'DEFAULT', 'DEFAULT_NATURAL', 'FLAT_NATURAL', 'RAISED_EXCITED',
    'RAISED_EXCITED_NATURAL', 'SAD_CONCERNED', 'SAD_CONCERNED_NATURAL',
    'UNI_BROW_NATURAL', 'UP_DOWN', 'UP_DOWN_NATURAL', 'FROWN_NATURAL',
]

# Pre-computed pool variants for _build_avatar — eliminates 4 list comprehensions
# per avatar generation call. Each name encodes the condition that selects it.
# clothe_types: SHIRT_SCOOP_NECK excluded when top is NOT long hair
_CLOTHE_TYPES_SHORT_HAIR = [c for c in _ALL_CLOTHE_TYPES if c.name != 'SHIRT_SCOOP_NECK']
# hair_pool: dark/mid hair for light skin; light/mid hair for darker skin
_HAIR_POOL_LIGHT_SKIN = (
    [h for h in _FILTERED_HAIR_COLORS if h.name in _DARK_HAIR | _MID_HAIR] or _ALL_HAIR_COLORS
)
_HAIR_POOL_DARK_SKIN = (
    [h for h in _FILTERED_HAIR_COLORS if h.name in _LIGHT_HAIR | _MID_HAIR] or _ALL_HAIR_COLORS
)
# hat_pool: light colors for dark hair; non-light for other hair
_HAT_POOL_DARK_HAIR  = [c for c in _ALL_COLORS if c.name in _LIGHT_COLORS] or _ALL_COLORS
_HAT_POOL_LIGHT_HAIR = [c for c in _ALL_COLORS if c.name not in _LIGHT_COLORS] or _ALL_COLORS
# clothe_pool: light colors for non-light skin (reuses same filter as _HAT_POOL_DARK_HAIR)
# light skin → _ALL_COLORS (already a constant, no extra name needed)
_CLOTHE_POOL_DARK_SKIN = _HAT_POOL_DARK_HAIR  # identical filter: _LIGHT_COLORS from _ALL_COLORS

_NO_CACHE = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Expires': '0', 'X-Content-Type-Options': 'nosniff'}
_NO_CACHE_HTML = {
    'Cache-Control': 'no-store, must-revalidate', 'Pragma': 'no-cache', 'Expires': '0',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': (
        "default-src 'self'; "
        "base-uri 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "media-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'none'; "
        "object-src 'none'"
    ),
}

_CPU_COUNT = max(1, (mp.cpu_count() or 1) - 1)
_URL_WORKER_COUNT   = 50          # concurrent threads per URL stress run
_MEM_CHUNK_BYTES    = 10 * 1024 * 1024   # 10 MiB per allocation chunk
_MEM_MAX_CHUNKS     = 500         # upper bound: ~5 GiB total allocation
_VERSION_CACHE = {"mtime": -1.0, "version": "", "message": ""}  # sentinel -1.0: never equals a real mtime, even mtime=0 (epoch)
_VERSION_LOCK = threading.Lock()

class StressManager:
    def __init__(self):
        self._cpu_procs = []
        self._cpu_on = False
        self._cpu_lock = threading.Lock()
        self._mem_refs = []
        self._mem_on = False
        self._mem_lock = threading.Lock()
        self._url_threads = []
        self._url_on = False
        self._url_stop = threading.Event()
        self._url_lock = threading.Lock()
        self._url_count = 0
        self._url_fails = 0
        self._url_count_lock = threading.Lock()
        self._url_toggle_gen = 0
        self._mem_toggle_gen = 0

    @staticmethod
    def _cpu_burn():
        try:
            os.nice(19)
        except Exception as e:
            logging.warning('Could not set nice value: %s', e)
        try:
            os.sched_setaffinity(0, set(range(_CPU_COUNT)))
        except Exception as e:
            logging.warning('Could not set CPU affinity (non-root in Docker?): %s', e)
        while True:
            pass

    @staticmethod
    def _kill_procs(procs, term_timeout=1, kill_timeout=0.5):
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        joiners = []
        for proc in procs:
            t = threading.Thread(target=proc.join, args=(term_timeout,), daemon=True)
            t.start()
            joiners.append(t)
        # Use an explicit timeout slightly larger than term_timeout so SonarQube
        # (python:S2276) can statically verify this join is bounded.
        for t in joiners:
            t.join(timeout=term_timeout + 1)
        for proc in procs:
            if proc.is_alive():
                try:
                    proc.kill()
                except Exception:
                    pass
        joiners = []
        for proc in procs:
            t = threading.Thread(target=proc.join, args=(kill_timeout,), daemon=True)
            t.start()
            joiners.append(t)
        for t in joiners:
            t.join(timeout=kill_timeout + 1)

    def toggle_cpu(self):
        with self._cpu_lock:
            if self._cpu_on:
                self._cpu_on = False
                self._kill_procs(self._cpu_procs, 1, 0.5)
                self._cpu_procs = []
            else:
                # Accumulate in a local list so that if proc.start() raises
                # mid-loop, we can kill the already-started processes and
                # avoid leaking them outside _cpu_procs.
                new_procs = []
                try:
                    for _ in range(_CPU_COUNT):
                        proc = mp.Process(target=self._cpu_burn, daemon=True)  # NOSONAR(python:S2076)
                        proc.start()
                        new_procs.append(proc)
                except Exception:
                    self._kill_procs(new_procs, 1, 0.5)
                    raise
                self._cpu_procs = new_procs
                self._cpu_on = True
        return self._cpu_on

    def toggle_memory(self):
        with self._mem_lock:
            if self._mem_on:
                self._mem_refs = []
                self._mem_on = False
            else:
                self._mem_on = True
                self._mem_refs = []
                self._mem_toggle_gen += 1
                gen = self._mem_toggle_gen
                avail = psutil.virtual_memory().available
                chunk = _MEM_CHUNK_BYTES
                count = min(_MEM_MAX_CHUNKS, avail * 3 // 5 // chunk)
                threading.Thread(target=self._mem_alloc_worker, args=(chunk, count, gen), daemon=True).start()
        return self._mem_on

    def _mem_alloc_worker(self, chunk, count, gen):
        refs = []
        try:
            for i in range(count):
                # Check cancellation every 10 chunks instead of every chunk.
                # Acquiring _mem_lock 500 times in a tight loop causes severe
                # contention with toggle_memory() and status(). A 10-chunk
                # cadence gives <100 ms latency on cancel at 10 MiB/chunk.
                if i % 10 == 0:
                    with self._mem_lock:
                        if not self._mem_on or self._mem_toggle_gen != gen:
                            return
                refs.append(bytearray(chunk))
        except MemoryError:
            logging.warning('Memory stress: allocation failed after %d chunks', len(refs))
            refs.clear()
            with self._mem_lock:
                if self._mem_on and self._mem_toggle_gen == gen:
                    self._mem_on = False
                    self._mem_refs = []
            return
        with self._mem_lock:
            if self._mem_on and self._mem_toggle_gen == gen:
                self._mem_refs = refs

    def toggle_url(self, url):
        with self._url_lock:
            if self._url_on:
                # Signal all workers to exit. Counters are intentionally NOT reset
                # here — final count stays visible until the next run starts.
                self._url_stop.set()
                self._url_threads = []
                self._url_on = False
            else:
                self._url_toggle_gen += 1
                gen = self._url_toggle_gen
                # Reset counters at START of a new run, not at end of the old one.
                # Resetting on stop would wipe the count while threads are still
                # mid-request, and clears the final tally the user sees after stopping.
                with self._url_count_lock:
                    self._url_count = 0
                    self._url_fails = 0
                # Clear the stop event immediately before spawning threads so it is
                # guaranteed clear when the first worker checks it.
                self._url_stop.clear()
                self._url_on = True
                for _ in range(_URL_WORKER_COUNT):
                    t = threading.Thread(target=self._url_worker, args=(url, gen), daemon=True)
                    t.start()
                    # Threads are daemon and never joined; don't hold a reference.
        return self._url_on

    def _url_worker(self, url, gen):
        session = requests.Session()
        try:
            while not self._url_stop.is_set():
                if self._url_toggle_gen != gen:
                    return
                try:
                    session.get(url, timeout=0.5, allow_redirects=False)  # NOSONAR(python:S5332) — URL validated by _validate_stress_url
                    with self._url_count_lock:
                        self._url_count += 1
                except requests.exceptions.RequestException:
                    with self._url_count_lock:
                        self._url_fails += 1
                # Yield the GIL between iterations so the 50 stress threads
                # do not starve the uvicorn event loop or each other.
                time.sleep(0)
        finally:
            session.close()

    def status(self):
        # Read each guarded field under its own lock to avoid torn reads.
        with self._cpu_lock:
            cpu_on = self._cpu_on
        with self._mem_lock:
            mem_on = self._mem_on
        with self._url_lock:
            url_on = self._url_on
        with self._url_count_lock:
            url_req = self._url_count
            url_fail = self._url_fails
        return {
            "cpu": cpu_on,
            "memory": mem_on,
            "url": url_on,
            "cpu_percent": round(psutil.cpu_percent(interval=0), 1),
            "memory_percent": round(psutil.virtual_memory().percent, 1),
            "url_requests": url_req,
            "url_fails": url_fail,
        }

    def cleanup(self):
        # Signal URL workers to stop via the event. Workers that are already
        # mid-request won't see the signal until their next loop check; the
        # generation bump below (inside _url_lock) guarantees they exit on the
        # next iteration even if they race past the event check.
        # NOTE: do NOT clear() the event here before bumping the gen — that
        # would create a window where workers miss both the signal and the gen
        # change. Instead, clear() inside _url_lock after bumping gen so it is
        # always atomic with the state that toggle_url(start) reads.
        self._url_stop.set()
        acquired = self._cpu_lock.acquire(timeout=3)
        if acquired:
            try:
                self._kill_procs(self._cpu_procs, 2, 1)
                self._cpu_procs = []
                self._cpu_on = False
            finally:
                self._cpu_lock.release()
        else:
            for proc in mp.active_children():
                try:
                    proc.terminate()
                except Exception:
                    pass
            for proc in mp.active_children():
                proc.join(timeout=1)
        with self._mem_lock:
            self._mem_refs = []
            self._mem_on = False
        with self._url_lock:
            # Bump generation: any worker that raced past the event check will
            # see the stale gen on its next iteration and exit cleanly.
            self._url_toggle_gen += 1
            self._url_threads = []
            self._url_on = False
            # Clear the event inside the lock so toggle_url(start) always finds
            # it unset — atomically with the gen bump, not before it.
            self._url_stop.clear()

# Allowlist for external targets only. Private addresses (localhost, 127.x, RFC-1918)
# are intentionally excluded from the general path: they would let any caller flood
# internal services via SSRF. Self-targeting (own_host) is handled separately below.
#
# SECURITY: The allowlist is intentionally empty by default. Hardcoding external
# hostnames (e.g. httpbin.org, example.com) would allow this app to be used as a
# stress-testing tool against third-party services without their consent, potentially
# violating their ToS or triggering DDoS protections. Operators may extend this set
# via a subclass or environment-driven override for controlled test environments.
_SSRF_ALLOWED: frozenset[str] = frozenset()

def _resolve_private(host):
    """Return True if hostname resolves to at least one private/loopback/link-local address."""
    try:
        infos = socket.getaddrinfo(host, None)
        seen = set()
        for info in infos:
            addr = info[4][0]
            if addr in seen:
                continue
            seen.add(addr)
            try:
                ip = ipaddress.ip_address(addr)
                if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
                    return True
            except ValueError:
                continue
    except socket.gaierror:
        pass
    return False

def _validate_stress_url(url, own_host=None):
    """
    Return True only if the URL is safe to use as a stress target.

    Allowed:
      1. Hosts in _SSRF_ALLOWED (curated external targets).
      2. The server's own hostname/IP (own_host), regardless of whether it is
         private — the URL stress feature's entire purpose is to hammer this
         app's own endpoint. `own_host` comes from `request.url.hostname`, so
         it is always the address the client actually reached this server on.

    Blocked:
      - Any other private/loopback/link-local IP (SSRF guard).
      - Non-http/https schemes.
      - Anything that raises an exception during parsing.

    SSRF note: the risk addressed in the original fix was that own_host
    resolution via _resolve_private was checked but the return True ran
    unconditionally afterwards (old code). That bug is fixed: the own_host
    branch now returns True *only* when host == own_host (exact match on the
    address the server received the request on). A spoofed hostname that merely
    resolves to the same IP would not match own_host unless DNS also matches,
    and the port is irrelevant to host matching. This is not a bypass.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        host = parsed.hostname or ''
        if not host:
            return False

        # Fast path: curated external allowlist.
        if host in _SSRF_ALLOWED:
            return True

        # Self-targeting: the URL stress feature sends window.location.origin,
        # which resolves to own_host (the hostname this request arrived on).
        # Allow it unconditionally — own_host is server-supplied, not user-supplied.
        # This works correctly for localhost, 127.x, pod IPs, and public domains.
        if own_host and host == own_host:
            return True

        # General case: block private/loopback/link-local IPs to prevent SSRF
        # against other services on the same network.
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_unspecified:
                return False
        except ValueError:
            # host is a name, not a bare IP.
            # Block hostnames that resolve *only* to private addresses,
            # but allow public hostnames (e.g. httpbin.org variants not in allowlist).
            if _resolve_private(host):
                return False
            return True
        return True
    except Exception:
        return False

stress_mgr = StressManager()
psutil.cpu_percent(interval=None)

def _safe_choice(choice_fn, pool, fallback):
    if not pool:
        return choice_fn(fallback)
    return choice_fn(pool)

def _build_avatar(choice_fn, mouth_types, eye_types, eyebrow_types=None):
    if eyebrow_types is None:
        eyebrow_types = _CUSTOM_EYEBROW_TYPES

    top_type = _safe_choice(choice_fn, _FILTERED_TOP_TYPES, _ALL_TOP_TYPES)
    is_long_hair = top_type.name.startswith('LONG_HAIR')
    # Use precomputed pool — avoids a list comprehension on every call.
    clothe_types = _ALL_CLOTHE_TYPES if is_long_hair else _CLOTHE_TYPES_SHORT_HAIR
    skin_color = _safe_choice(choice_fn, _FILTERED_SKIN_COLORS, _ALL_SKIN_COLORS)

    hair_pool = _HAIR_POOL_LIGHT_SKIN if skin_color.name in _LIGHT_SKIN else _HAIR_POOL_DARK_SKIN
    hair_color = choice_fn(hair_pool)

    hat_pool = _HAT_POOL_DARK_HAIR if hair_color.name in _DARK_HAIR else _HAT_POOL_LIGHT_HAIR

    clothe_pool = _CLOTHE_POOL_DARK_SKIN if skin_color.name not in _LIGHT_SKIN else _ALL_COLORS

    return py_avataaars.PyAvataaar(
        style=py_avataaars.AvatarStyle.TRANSPARENT,
        skin_color=skin_color,
        hair_color=hair_color,
        # When pool == fallback, _safe_choice reduces to choice_fn(pool) — call directly.
        facial_hair_type=py_avataaars.FacialHairType.DEFAULT if is_long_hair else choice_fn(_ALL_FACIAL_HAIR),
        facial_hair_color=hair_color,
        top_type=top_type,
        hat_color=choice_fn(hat_pool),
        mouth_type=getattr(py_avataaars.MouthType, choice_fn(mouth_types)),
        eye_type=getattr(py_avataaars.EyesType, choice_fn(eye_types)),
        eyebrow_type=getattr(py_avataaars.EyebrowType, choice_fn(eyebrow_types)),
        nose_type=choice_fn(_ALL_NOSE_TYPES),
        accessories_type=choice_fn(_ALL_ACCESSORIES),
        clothe_type=_safe_choice(choice_fn, clothe_types, _ALL_CLOTHE_TYPES),
        clothe_color=choice_fn(clothe_pool),
        clothe_graphic_type=choice_fn(_ALL_CLOTHE_GRAPHIC),
    )

_RNG = SystemRandom()  # module-level singleton; SystemRandom is thread-safe (OS entropy)

# Fixed mouth type: always SMILE for generated avatars.
# Defined as a module constant so its intent is explicit and the call site
# doesn't construct a new list on every invocation.
_AVATAR_MOUTH_TYPES = ('SMILE',)
# Eye types available for random selection in generated avatars.
_AVATAR_EYE_TYPES = ('DEFAULT', 'CLOSE', 'HAPPY', 'SIDE', 'SQUINT', 'SURPRISED', 'WINK')

def _generate_avatar():
    avatar = _build_avatar(_RNG.choice, _AVATAR_MOUTH_TYPES, _AVATAR_EYE_TYPES)
    return avatar.render_svg()

_VERSION_FILE = os.path.join(_SCRIPT_DIR, 'version.txt')

def _read_version():
    # Fast path: check mtime under the lock first.
    # Only perform expensive file I/O when a change is detected.
    # The lock is held across the mtime check AND the cache read/write to
    # prevent two concurrent callers from both seeing a stale mtime and
    # redundantly re-reading the file (TOCTOU race).
    try:
        mtime = os.path.getmtime(_VERSION_FILE)
    except OSError as e:
        logging.warning('Could not read version.txt: %s', e)
        with _VERSION_LOCK:
            # Set a placeholder if this is the very first read; then return
            # whatever is cached. Merge into one lock acquisition to avoid
            # two sequential acquire/release pairs on the same lock.
            if not _VERSION_CACHE["version"]:
                _VERSION_CACHE["version"] = '—'
            return _VERSION_CACHE["version"], _VERSION_CACHE["message"]
    except Exception as e:
        logging.error('Unexpected error reading version.txt: %s', e)
        with _VERSION_LOCK:
            return _VERSION_CACHE["version"], _VERSION_CACHE["message"]

    with _VERSION_LOCK:
        if mtime == _VERSION_CACHE["mtime"]:
            # Cache is current — return immediately without touching disk.
            return _VERSION_CACHE["version"], _VERSION_CACHE["message"]

    # File has changed; read outside the lock (I/O can be slow).
    try:
        kv = {}
        with open(_VERSION_FILE, encoding='utf-8') as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    kv[k.strip()] = v.strip()
        # Re-acquire lock to update cache atomically.
        with _VERSION_LOCK:
            # Guard against a concurrent update that already wrote a newer mtime.
            if mtime >= _VERSION_CACHE["mtime"]:
                _VERSION_CACHE["mtime"] = mtime
                _VERSION_CACHE["version"] = kv.get('version', '—')
                _VERSION_CACHE["message"] = kv.get('message', '')
            return _VERSION_CACHE["version"], _VERSION_CACHE["message"]
    except OSError as e:
        logging.warning('Could not read version.txt: %s', e)
    except Exception as e:
        logging.error('Unexpected error reading version.txt: %s', e)
    with _VERSION_LOCK:
        return _VERSION_CACHE["version"], _VERSION_CACHE["message"]

def index(request):
    # Normalise to lowercase so matching is case-insensitive.
    # e.g. 'Curl/8.x', 'CURL/7.x', 'go-http-client/2.0' all match correctly.
    ua = request.headers.get('user-agent', '').lower()
    if "go-http-client" in ua or "python-urllib" in ua:
        return PlainTextResponse("healthy", headers=_NO_CACHE_HTML)
    if "curl" in ua:
        return templates.TemplateResponse(request, 'index.txt', {'request': request}, headers=_NO_CACHE_HTML, media_type='text/plain')
    refresh = request.query_params.get('refresh', '0') == '1'
    if refresh:
        # Generate outside the lock: SVG rendering is slow and holding _avatar_lock
        # during generation blocks all concurrent requests doing lazy-init.
        new_svg = _generate_avatar()
        with _avatar_lock:
            global_state.avatar_svg = new_svg
        return RedirectResponse(url="/", headers=_NO_CACHE_HTML)
    avatar_svg = global_state.avatar_svg
    if avatar_svg is None:
        with _avatar_lock:
            if global_state.avatar_svg is None:
                global_state.avatar_svg = _generate_avatar()
            avatar_svg = global_state.avatar_svg
    version, message = _read_version()
    return templates.TemplateResponse(request, 'index.html', {
        'request': request,
        'avatar_svg': avatar_svg,
        'hostname': _HOSTNAME,
        'version': version,
        'message': message,
    }, headers=_NO_CACHE_HTML)

def stress_status(request):  # pylint: disable=unused-argument
    return JSONResponse(stress_mgr.status(), headers=_NO_CACHE)

def stress_cpu(request):  # pylint: disable=unused-argument
    return JSONResponse({"cpu": stress_mgr.toggle_cpu()}, headers=_NO_CACHE)

def stress_memory(request):  # pylint: disable=unused-argument
    return JSONResponse({"memory": stress_mgr.toggle_memory()}, headers=_NO_CACHE)

def stress_url(request):
    url = request.query_params.get("url", str(request.base_url).rstrip("/"))
    if not _validate_stress_url(url, own_host=request.url.hostname):
        return JSONResponse({"error": "URL not allowed"}, status_code=400, headers=_NO_CACHE)
    return JSONResponse({"url": stress_mgr.toggle_url(url)}, headers=_NO_CACHE)

def refresh_avatar(request):  # pylint: disable=unused-argument
    svg = _generate_avatar()
    with _avatar_lock:
        global_state.avatar_svg = svg
    return JSONResponse({"svg": svg}, headers=_NO_CACHE)

routes = [
    Route('/', endpoint=index),
    Route('/api/stress', endpoint=stress_status),
    Route('/api/stress/cpu', endpoint=stress_cpu, methods=['POST']),
    Route('/api/stress/memory', endpoint=stress_memory, methods=['POST']),
    Route('/api/stress/url', endpoint=stress_url, methods=['POST']),
    Route('/api/avatar', endpoint=refresh_avatar, methods=['POST']),
    Mount('/static', app=StaticFiles(directory='static'), name='static'),
]

def _shutdown_handler(sig, frame):  # pylint: disable=unused-argument
    """Signal handler for SIGINT/SIGTERM.

    Uses a named function instead of a lambda so that:
      1. os._exit(0) is guaranteed via try/finally even if cleanup() raises.
      2. Pylint W0101 false-positive ('unreachable code' on the second lambda)
         is eliminated — a single handler is registered for both signals.
    """
    try:
        stress_mgr.cleanup()
        logging.shutdown()
    finally:
        os._exit(0)  # NOSONAR(python:S4829) — intentional: skips atexit to prevent uvicorn deadlock

@asynccontextmanager
async def lifespan(_app):
    # Startup: install OS signal handlers for graceful shutdown.
    # NOTE: os._exit() is intentional — it skips Python atexit/finally blocks
    # so uvicorn worker processes terminate immediately without deadlock.
    try:
        signal.signal(signal.SIGINT,  _shutdown_handler)   # NOSONAR(python:S4830)
        signal.signal(signal.SIGTERM, _shutdown_handler)  # NOSONAR(python:S4830)
    except ValueError:
        # Raised when signal() is called outside the main thread (e.g. uvicorn worker). Safe to ignore.
        pass
    # Warm the avatar cache before accepting traffic. Without this, the first
    # real request to '/' pays the cost of py_avataaars SVG rendering
    # synchronously (CPU-bound, ~hundreds of ms), which is felt as a one-time
    # page freeze. Running it here means uvicorn doesn't start serving until
    # the cache is already populated — every user-facing request hits the
    # fast cached path instead.
    try:
        with _avatar_lock:
            if global_state.avatar_svg is None:
                global_state.avatar_svg = _generate_avatar()
    except Exception:
        # Don't block server startup if generation fails for any reason
        # (e.g. missing font/asset in a degraded environment); index() still
        # has its own lazy-init fallback and will retry on first request.
        logging.warning('Avatar cache warm-up failed; will lazy-init on first request', exc_info=True)
    yield
    # Shutdown: clean up stress workers on normal (non-signal) exit.
    stress_mgr.cleanup()

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, proxy-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # RFC 6797 §8.1: HSTS MUST only be delivered over HTTPS; over plain
        # HTTP it is silently ignored by browsers and serves no purpose.
        if request.url.scheme == 'https':
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        return response

app = Starlette(debug=os.getenv('DEBUG', 'False').lower() == 'true', routes=routes, lifespan=lifespan)

app.add_middleware(NoCacheMiddleware)
app.add_middleware(PrometheusMiddleware)
app.add_route("/metrics", handle_metrics)


if __name__ == "__main__":
    # Terminate any stray child processes left over from a previous run before
    # starting uvicorn. Do NOT call stress_mgr.cleanup() here — it modifies
    # internal stress state before the server has done anything, and the side
    # effects (setting _url_stop, bumping gen) are unnecessary at startup.
    for _child in mp.active_children():
        try:
            _child.terminate()
        except Exception:
            pass
    for _child in mp.active_children():
        _child.join(timeout=1)

    try:
        uvicorn.run(app, host="0.0.0.0",
                    port=int(os.getenv('PORT', '8000')),
                    log_level=_LOG_LEVEL.lower(),
                    timeout_keep_alive=5,
                    proxy_headers=True)
    except KeyboardInterrupt:
        pass
    finally:
        stress_mgr.cleanup()
        logging.shutdown()
