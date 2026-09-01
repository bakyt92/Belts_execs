"""
Client for French company data: INSEE Sirene API + Recherche d'entreprises API.

Handles OAuth token caching, rate limiting, 429 backoff and cursor pagination.

Environment:
    INSEE_CLIENT_ID
    INSEE_CLIENT_SECRET

Dependencies:
    pip install requests
"""

from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://portail-api.insee.fr/token"
SIRENE_URL = "https://api.insee.fr/api-sirene/3.11"
RECHERCHE_URL = "https://recherche-entreprises.api.gouv.fr"

USER_AGENT = "sirene-client/1.0 (+contact@example.org)"


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window limiter. Thread-safe, blocks until a slot frees up."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0])
            time.sleep(max(wait, 0.01))


# --------------------------------------------------------------------------
# Shared HTTP behaviour
# --------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def _request(
    session: requests.Session,
    method: str,
    url: str,
    limiter: RateLimiter,
    *,
    max_retries: int = 4,
    **kwargs: Any,
) -> requests.Response:
    """Rate-limited request with Retry-After aware backoff on 429 / 5xx."""
    for attempt in range(max_retries + 1):
        limiter.acquire()
        resp = session.request(method, url, timeout=30, **kwargs)

        if resp.status_code == 429:
            delay = float(resp.headers.get("Retry-After", 2**attempt))
            log.warning("429 on %s, sleeping %.1fs", url, delay)
            time.sleep(delay)
            continue

        if resp.status_code >= 500 and attempt < max_retries:
            delay = 2**attempt
            log.warning("HTTP %s on %s, retrying in %ss", resp.status_code, url, delay)
            time.sleep(delay)
            continue

        if resp.status_code >= 400:
            raise ApiError(resp.status_code, resp.text)

        return resp

    raise ApiError(429, f"exhausted {max_retries} retries on {url}")


# --------------------------------------------------------------------------
# INSEE Sirene (authenticated)
# --------------------------------------------------------------------------


@dataclass
class _Token:
    value: str | None = None
    expires_at: float = 0.0


class SireneClient:
    """
    Authenticated INSEE Sirene client.

    Authoritative and complete (includes non-diffusible records and full
    period history), but capped at 30 requests/minute with no paid tier.
    Use for exact lookups, not for a search box.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        calls_per_minute: int = 28,  # headroom under the documented 30
    ):
        self.client_id = client_id or os.environ["INSEE_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["INSEE_CLIENT_SECRET"]
        self._token = _Token()
        self._token_lock = threading.Lock()
        self._limiter = RateLimiter(calls_per_minute, 60.0)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    # -- auth --------------------------------------------------------------

    def _access_token(self) -> str:
        with self._token_lock:
            # 60s margin so a token can't expire mid-flight
            if self._token.value and time.time() < self._token.expires_at - 60:
                return self._token.value

            resp = self._session.post(
                TOKEN_URL,
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                timeout=15,
            )
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, resp.text)
            data = resp.json()
            self._token.value = data["access_token"]
            self._token.expires_at = time.time() + int(data.get("expires_in", 3600))
            log.info("obtained INSEE token, valid %ss", data.get("expires_in"))
            return self._token.value

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        resp = _request(
            self._session,
            "GET",
            f"{SIRENE_URL}{path}",
            self._limiter,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            params=params,
        )
        return resp.json()

    # -- lookups -----------------------------------------------------------

    def get_siret(self, siret: str) -> dict:
        """Single establishment by 14-digit SIRET."""
        return self._get(f"/siret/{siret}")["etablissement"]

    def get_siren(self, siren: str) -> dict:
        """Single legal unit by 9-digit SIREN."""
        return self._get(f"/siren/{siren}")["uniteLegale"]

    # -- search ------------------------------------------------------------

    def search_etablissements(
        self,
        query: str,
        *,
        fields: list[str] | None = None,
        page_size: int = 1000,
        max_results: int | None = None,
    ) -> Iterator[dict]:
        """
        Cursor-paginated establishment search.

        `query` uses Sirene's field syntax, e.g.
            activitePrincipaleEtablissement:56.10A AND etatAdministratifEtablissement:A

        Yields establishments lazily; safe for large result sets.
        """
        cursor = "*"
        seen = 0
        while True:
            params: dict[str, Any] = {
                "q": query,
                "nombre": min(page_size, 1000),
                "curseur": cursor,
            }
            if fields:
                params["champs"] = ",".join(fields)

            payload = self._get("/siret", params)
            header = payload.get("header", {})
            batch = payload.get("etablissements", [])

            for item in batch:
                yield item
                seen += 1
                if max_results and seen >= max_results:
                    return

            next_cursor = header.get("curseurSuivant")
            if not batch or not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor

    def count(self, query: str) -> int:
        """Total matches without pulling results."""
        payload = self._get("/siret", {"q": query, "nombre": 1})
        return int(payload.get("header", {}).get("total", 0))


# --------------------------------------------------------------------------
# Recherche d'entreprises (open, no auth)
# --------------------------------------------------------------------------


class RechercheClient:
    """
    Open full-text company search. No auth, 7 req/s per IP.

    Carries directors and coarse financials that Sirene lacks, but caps
    pagination at 10,000 results per query.
    """

    PAGE_CEILING = 10_000

    def __init__(self, calls_per_second: int = 5):
        self._limiter = RateLimiter(calls_per_second, 1.0)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def search(
        self,
        q: str | None = None,
        *,
        activite_principale: str | list[str] | None = None,
        section_activite_principale: str | None = None,
        code_postal: str | None = None,
        departement: str | None = None,
        etat_administratif: str | None = None,
        minimal: bool = False,
        include: list[str] | None = None,
        per_page: int = 25,
        page: int = 1,
        **extra: Any,
    ) -> dict:
        """Single page of results. See the API docs for the full filter list."""
        if isinstance(activite_principale, list):
            activite_principale = ",".join(activite_principale)

        params: dict[str, Any] = {
            "q": q,
            "activite_principale": activite_principale,
            "section_activite_principale": section_activite_principale,
            "code_postal": code_postal,
            "departement": departement,
            "etat_administratif": etat_administratif,
            "per_page": min(per_page, 25),
            "page": page,
            **extra,
        }
        if minimal:
            params["minimal"] = "true"
            if include:
                params["include"] = ",".join(include)

        params = {k: v for k, v in params.items() if v is not None}
        resp = _request(
            self._session, "GET", f"{RECHERCHE_URL}/search", self._limiter, params=params
        )
        return resp.json()

    def iter_all(self, **kwargs: Any) -> Iterator[dict]:
        """
        Page through every result for a query.

        Warns only if pagination actually runs into the 10,000-result
        ceiling — not merely because more matches exist than you asked for.
        If you hit it, narrow the query (by departement, then code_postal)
        and merge the slices.
        """
        kwargs.setdefault("per_page", 25)
        page = 1
        yielded = 0
        total = 0
        while True:
            payload = self.search(page=page, **kwargs)
            results = payload.get("results", [])
            if not results:
                break

            total = payload.get("total_results", 0)
            yield from results
            yielded += len(results)

            if page >= payload.get("total_pages", 1):
                break
            page += 1

        if yielded >= self.PAGE_CEILING and total >= self.PAGE_CEILING:
            log.warning(
                "pagination stopped at the %s-result ceiling (%s matches exist); "
                "narrow by departement or code_postal to get the rest",
                self.PAGE_CEILING,
                total,
            )

    def near_point(self, lat: float, long: float, radius: float = 5, **kwargs: Any) -> dict:
        """Geographic search around a coordinate."""
        params = {"lat": lat, "long": long, "radius": radius, **kwargs}
        resp = _request(
            self._session,
            "GET",
            f"{RECHERCHE_URL}/near_point",
            self._limiter,
            params={k: v for k, v in params.items() if v is not None},
        )
        return resp.json()


# --------------------------------------------------------------------------
# NAF reference table
# --------------------------------------------------------------------------


@dataclass
class NafEntry:
    code: str
    libelle: str

    @property
    def division(self) -> str:
        return self.code[:2]


@dataclass
class NafTable:
    """
    NAF/APE code lookup, so users can search by words instead of codes.

    Load from INSEE's published CSV (two columns: code, libelle).
    """

    entries: dict[str, NafEntry] = field(default_factory=dict)

    # Section letter -> division ranges. Sections aren't stored in Sirene,
    # so expand them to divisions yourself when querying.
    SECTIONS: dict[str, tuple[int, int]] = field(
        default_factory=lambda: {
            "A": (1, 3), "B": (5, 9), "C": (10, 33), "D": (35, 35),
            "E": (36, 39), "F": (41, 43), "G": (45, 47), "H": (49, 53),
            "I": (55, 56), "J": (58, 63), "K": (64, 66), "L": (68, 68),
            "M": (69, 75), "N": (77, 82), "O": (84, 84), "P": (85, 85),
            "Q": (86, 88), "R": (90, 93), "S": (94, 96), "T": (97, 98),
            "U": (99, 99),
        }
    )

    @staticmethod
    def normalise(code: str) -> str:
        """`5610A` and `56.10A` both become `56.10A`."""
        c = code.replace(".", "").replace(" ", "").upper()
        return f"{c[:2]}.{c[2:]}" if len(c) > 2 else c

    @classmethod
    def from_csv(cls, path: str, code_col: str = "code", label_col: str = "libelle") -> NafTable:
        table = cls()
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                code = cls.normalise(row[code_col])
                table.entries[code] = NafEntry(code, row[label_col].strip())
        return table

    def lookup(self, code: str) -> NafEntry | None:
        return self.entries.get(self.normalise(code))

    def search(self, term: str, limit: int = 20) -> list[NafEntry]:
        """Word search over labels — 'boulangerie' -> the codes that match."""
        needle = term.casefold()
        hits = [e for e in self.entries.values() if needle in e.libelle.casefold()]
        hits.sort(key=lambda e: (len(e.libelle), e.code))
        return hits[:limit]

    def section_codes(self, section: str) -> list[str]:
        """All codes belonging to a section letter."""
        lo, hi = self.SECTIONS[section.upper()]
        return sorted(e.code for e in self.entries.values() if lo <= int(e.division) <= hi)


# --------------------------------------------------------------------------
# Activity search across sources
# --------------------------------------------------------------------------


def search_by_activity(
    term: str,
    naf: NafTable,
    *,
    departement: str | None = None,
    code_postal: str | None = None,
    client: RechercheClient | None = None,
    max_codes: int = 5,
    max_results: int = 100,
) -> dict[str, Any]:
    """
    Two-step activity search: words -> NAF codes -> companies.

    Filtering on raw codes only works for people who already know them,
    so resolve the user's words to codes first and show both.
    """
    client = client or RechercheClient()
    codes = naf.search(term, limit=max_codes)
    if not codes:
        return {"term": term, "codes": [], "results": []}

    results: list[dict] = []
    for item in client.iter_all(
        activite_principale=[e.code for e in codes],
        departement=departement,
        code_postal=code_postal,
        etat_administratif="A",
        minimal=True,
        include=["siege"],
    ):
        results.append(item)
        if len(results) >= max_results:
            break

    return {
        "term": term,
        "codes": [{"code": e.code, "libelle": e.libelle} for e in codes],
        "results": results,
    }


# --------------------------------------------------------------------------
# Sirene query builder
# --------------------------------------------------------------------------


def sirene_query(**criteria: Any) -> str:
    """
    Build a Sirene `q` expression.

        sirene_query(activitePrincipaleEtablissement="56.10A",
                     etatAdministratifEtablissement="A",
                     codePostalEtablissement="93500")

    Lists become OR groups; None values are dropped.
    """
    parts = []
    for field_name, value in criteria.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            group = " OR ".join(f"{field_name}:{v}" for v in value)
            parts.append(f"({group})")
        else:
            parts.append(f"{field_name}:{value}")
    return " AND ".join(parts)


# --------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Open API — works with no credentials.
    rech = RechercheClient()
    payload = rech.search(
        activite_principale="56.10A",
        departement="93",
        etat_administratif="A",
        minimal=True,
        include=["siege"],
        per_page=5,
    )
    print(f"{payload['total_results']} matches\n")
    for r in payload["results"]:
        siege = r.get("siege") or {}
        print(f"  {r['siren']}  {r['nom_complet'][:40]:<40} {siege.get('code_postal', '')}")

    # Authenticated Sirene — needs INSEE_CLIENT_ID / INSEE_CLIENT_SECRET.
    if os.environ.get("INSEE_CLIENT_ID"):
        sirene = SireneClient()
        q = sirene_query(
            activitePrincipaleEtablissement="56.10A",
            etatAdministratifEtablissement="A",
            codePostalEtablissement="93500",
        )
        print(f"\nSirene query: {q}")
        print(f"total: {sirene.count(q)}")
        for etab in sirene.search_etablissements(q, max_results=5):
            ul = etab.get("uniteLegale", {})
            print(f"  {etab['siret']}  {ul.get('denominationUniteLegale')}")