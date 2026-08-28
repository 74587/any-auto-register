"""Proxy pool backed by the application database."""

from datetime import datetime, timezone
import threading
from typing import Optional

from sqlmodel import Session, select

from .db import ProxyModel, engine
from .proxy_utils import build_requests_proxy_config, normalize_proxy_url


class ProxyPool:
    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()

    def _find_by_url(self, session: Session, url: str) -> ProxyModel | None:
        p = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
        if p:
            return p

        normalized = normalize_proxy_url(url)
        if not normalized:
            return None

        if normalized != url:
            p = session.exec(
                select(ProxyModel).where(ProxyModel.url == normalized)
            ).first()
            if p:
                return p

        for candidate in session.exec(select(ProxyModel)).all():
            if normalize_proxy_url(candidate.url) == normalized:
                return candidate
        return None

    def get_next(self, region: str = "") -> Optional[str]:
        """Return the next active proxy, biased toward higher success rate."""
        with Session(engine) as s:
            q = select(ProxyModel).where(ProxyModel.is_active == True)
            if region:
                q = q.where(ProxyModel.region == region)
            proxies = s.exec(q).all()
            if not proxies:
                return None
            proxies.sort(
                key=lambda p: p.success_count / max(p.success_count + p.fail_count, 1),
                reverse=True,
            )
            with self._lock:
                idx = self._index % len(proxies)
                self._index += 1
            return proxies[idx].url

    def report_success(self, url: str) -> None:
        with Session(engine) as s:
            p = self._find_by_url(s, url)
            if p:
                p.success_count += 1
                p.is_active = True
                p.last_checked = datetime.now(timezone.utc)
                s.add(p)
                s.commit()

    def report_fail(self, url: str) -> None:
        with Session(engine) as s:
            p = self._find_by_url(s, url)
            if p:
                p.fail_count += 1
                p.last_checked = datetime.now(timezone.utc)
                if p.fail_count > 0 and p.success_count == 0 and p.fail_count >= 5:
                    p.is_active = False
                s.add(p)
                s.commit()

    def check_all(self) -> dict:
        """Probe all configured proxies against a neutral endpoint."""
        import requests

        with Session(engine) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0}
        for p in proxies:
            try:
                r = requests.get(
                    "https://httpbin.org/ip",
                    proxies=build_requests_proxy_config(p.url),
                    timeout=8,
                )
                if r.status_code == 200:
                    self.report_success(p.url)
                    results["ok"] += 1
                    continue
            except Exception:
                pass
            self.report_fail(p.url)
            results["fail"] += 1
        return results


proxy_pool = ProxyPool()
