"""Журнал решений: что демон увидел и что сделал.

Зачем отдельный файл, если есть journald:

* journald может быть настроен как volatile (логи в /run, теряются при
  перезагрузке) — файл в ``~/.local/state`` переживает всё;
* по нему строится отчёт (``layout-fixer report``): видно не только
  исправления, но и **все проверенные слова** — а именно это нужно, чтобы
  оценить качество распознавания и ложные срабатывания;
* формат простой TSV, его легко посмотреть глазами и разобрать скриптом.

Формат строки:
    время  вид  слово  раскладка  исправление  целевая  уверенность  причина  приложение

Виды: FIX (исправлено), OBSERVE (в режиме наблюдения исправил бы),
CANCEL (решение было, но применить нельзя), SKIP (оставлено без правки),
REVERT (исправление введено, но пользователь сразу стёр — ложное срабатывание).

Приложение — процесс активного окна на момент правки (только на X11, см.
``x11focus``). Пока только наблюдение: по нему видно, в каких программах
автоправка нужна, а где вредна.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".local" / "state" / "layout-fixer"
DEFAULT_PATH = STATE_DIR / "decisions.log"

KINDS = ("FIX", "OBSERVE", "CANCEL", "SKIP", "REVERT")

KIND_TITLES = {
    "FIX": "исправлено",
    "OBSERVE": "было бы исправлено (режим наблюдения)",
    "CANCEL": "отменено",
    "SKIP": "оставлено без правки",
    "REVERT": "стёрто пользователем (ложное срабатывание)",
}


def _clean(text: str) -> str:
    """Убрать из поля табуляции/переводы строк, чтобы не ломать формат."""
    return text.replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


@dataclass(frozen=True)
class Entry:
    """Одна запись журнала."""

    timestamp: datetime
    kind: str
    word: str
    layout: str
    converted: str
    target: str
    confidence: float
    reason: str
    app: str = ""

    def __str__(self) -> str:
        moment = self.timestamp.strftime("%d.%m %H:%M:%S")
        if self.kind == "REVERT":
            return (
                f"{moment}  {self.kind:7} {self.word!r} ({self.layout}) -> "
                f"{self.converted!r} — стёрто пользователем"
            )
        if self.kind in ("FIX", "OBSERVE"):
            return f"{moment}  {self.kind:7} {self.word!r} ({self.layout}) -> {self.converted!r} ({self.target}) {self.confidence:.2f}"
        if self.kind == "CANCEL":
            return f"{moment}  {self.kind:7} {self.word!r} ({self.layout}) -> {self.converted!r}: {self.reason}"
        return f"{moment}  {self.kind:7} {self.word!r} ({self.layout}): {self.reason}"


class DecisionLog:
    """Дописывающий журнал решений."""

    def __init__(self, path: Path = DEFAULT_PATH, enabled: bool = True, keep_words: bool = True):
        self.path = path
        self.enabled = enabled
        # Слова — это текст, который набирает пользователь. Можно отключить
        # их запись, оставив только виды решений и причины.
        self.keep_words = keep_words
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("не удалось создать каталог журнала %s: %s", self.path.parent, exc)
                self.enabled = False

    def record(
        self,
        kind: str,
        word: str,
        layout: str,
        converted: str = "",
        target: str = "",
        confidence: float = 0.0,
        reason: str = "",
        app: str = "",
    ) -> None:
        if not self.enabled:
            return
        if not self.keep_words:
            word = f"<{len(word)} симв.>"
        line = "\t".join(
            [
                datetime.now().isoformat(timespec="seconds"),
                kind,
                _clean(word),
                _clean(layout),
                _clean(converted),
                _clean(target),
                f"{confidence:.2f}",
                _clean(reason),
                _clean(app),
            ]
        )
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            log.warning("не удалось записать в журнал решений: %s", exc)

    # --- чтение и отчёт -----------------------------------------------
    @staticmethod
    def read(path: Path = DEFAULT_PATH, days: int | None = None) -> list[Entry]:
        if not path.exists():
            return []
        since = datetime.now() - timedelta(days=days) if days else None
        entries: list[Entry] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split("\t")
            if len(parts) < 8:
                continue
            try:
                moment = datetime.fromisoformat(parts[0])
                confidence = float(parts[6])
            except ValueError:
                continue
            if since is not None and moment < since:
                continue
            entries.append(
                Entry(
                    timestamp=moment,
                    kind=parts[1],
                    word=parts[2],
                    layout=parts[3],
                    converted=parts[4],
                    target=parts[5],
                    confidence=confidence,
                    reason=parts[7],
                    app=parts[8] if len(parts) > 8 else "",
                )
            )
        return entries


def summarize(entries: list[Entry], top: int = 10, tail: int = 10) -> str:
    """Человекочитаемый отчёт по записям журнала."""
    if not entries:
        return "Записей нет. Демон либо не запускался, либо ещё ничего не проверил."

    lines: list[str] = []
    first, last = entries[0].timestamp, entries[-1].timestamp
    lines.append(f"Период: {first:%d.%m.%Y %H:%M} — {last:%d.%m.%Y %H:%M} ({len(entries)} записей)")

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    lines.append("")
    lines.append("Итоги:")
    for kind, title in KIND_TITLES.items():
        if kind in counts:
            lines.append(f"  {title:38} {counts[kind]}")

    fixed = [entry for entry in entries if entry.kind in ("FIX", "OBSERVE")]
    if fixed:
        pairs: dict[tuple[str, str], int] = {}
        for entry in fixed:
            pairs[(entry.word, entry.converted)] = pairs.get((entry.word, entry.converted), 0) + 1
        lines.append("")
        lines.append(f"Что исправлялось (топ {top}):")
        for (word, converted), count in sorted(pairs.items(), key=lambda item: -item[1])[:top]:
            lines.append(f"  {count:3}× {word!r} -> {converted!r}")

    cancelled = [entry for entry in entries if entry.kind == "CANCEL"]
    if cancelled:
        reasons: dict[str, int] = {}
        for entry in cancelled:
            reasons[entry.reason] = reasons.get(entry.reason, 0) + 1
        lines.append("")
        lines.append("Причины отмен:")
        for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
            lines.append(f"  {count:3}× {reason}")

    # Пользователь, стёрший исправление, — единственный источник правды о том,
    # какие правки были лишними: сам детектор уверен в своей правоте на 0.95.
    reverted = [entry for entry in entries if entry.kind == "REVERT"]
    applied = [entry for entry in entries if entry.kind == "FIX"]
    if reverted:
        pairs: dict[tuple[str, str], int] = {}
        for entry in reverted:
            key = (entry.word, entry.converted)
            pairs[key] = pairs.get(key, 0) + 1
        lines.append("")
        if applied:
            share = 100 * len(reverted) / len(applied)
            lines.append(
                f"Ложных срабатываний: {len(reverted)} из {len(applied)} "
                f"({share:.1f}% от исправлений)"
            )
        lines.append(f"Стёрто пользователем (топ {top}):")
        for (word, converted), count in sorted(pairs.items(), key=lambda item: -item[1])[:top]:
            lines.append(f"  {count:3}× {word!r} -> {converted!r}")

    if tail:
        lines.append("")
        lines.append(f"Последние {tail} записей:")
        for entry in entries[-tail:]:
            lines.append(f"  {entry}")

    return "\n".join(lines)
