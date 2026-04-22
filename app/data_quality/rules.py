"""Rules-based data quality engine with dynamic activation-date support."""
import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

_BOILERPLATE_SHORT_MIN_CHARS = 200


def _rule_boilerplate_short(job: dict) -> bool:
    """True when description is present but suspiciously short."""
    desc = (job.get("description") or "").strip()
    return bool(desc) and len(desc) < _BOILERPLATE_SHORT_MIN_CHARS


def _rule_no_title(job: dict) -> bool:
    """True when title is absent or blank."""
    return not (job.get("title") or "").strip()


def _rule_no_description(job: dict) -> bool:
    """True when description is absent or blank."""
    return not (job.get("description") or "").strip()


def _rule_no_url(job: dict) -> bool:
    """True when url is absent or blank."""
    return not (job.get("url") or "").strip()


# Registry: rule_name -> callable(job_dict) -> bool
_RULE_FN: dict[str, Callable[[dict], bool]] = {
    "boilerplate_short": _rule_boilerplate_short,
    "no_title": _rule_no_title,
    "no_description": _rule_no_description,
    "no_url": _rule_no_url,
}

# ---------------------------------------------------------------------------
# Activation-date helpers
# ---------------------------------------------------------------------------


def compute_activation_date(
    config: dict,
    today: date,
    activation_file: Path,
) -> date:
    """Return the reject-activation date.

    - If *activation_file* exists: read and return the stored ISO date.
    - If it does not exist: compute ``today + grace_period_days``, persist to
      file, and return.
    - If the file is corrupt (not a valid ISO date): raise ``ValueError`` with
      a descriptive message.
    """
    if activation_file.exists():
        raw = activation_file.read_text(encoding="utf-8").strip()
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(
                f"Activation file '{activation_file}' contains invalid ISO date "
                f"'{raw}': {exc}"
            ) from exc
        logger.info(
            "Activation date loaded from file",
            extra={"activation_date": str(parsed), "file": str(activation_file)},
        )
        return parsed

    grace_days: int = int(config.get("grace_period_days", 7))
    from datetime import timedelta

    activation = today + timedelta(days=grace_days)
    activation_file.parent.mkdir(parents=True, exist_ok=True)
    activation_file.write_text(activation.isoformat(), encoding="utf-8")
    logger.info(
        "Activation date computed and written",
        extra={
            "activation_date": str(activation),
            "grace_days": grace_days,
            "file": str(activation_file),
        },
    )
    return activation


def is_reject_active(now: datetime, activation: date) -> bool:
    """Return True when the current datetime is on or after *activation* date."""
    return now.date() >= activation


# ---------------------------------------------------------------------------
# Rules engine
# ---------------------------------------------------------------------------

Verdict = Literal["keep", "reject"]


class RulesEngine:
    """Classify jobs as keep/reject based on configurable rules.

    Config keys expected::

        rules:
          flag: [boilerplate_short, no_description]
          reject: [no_title, no_url]
          grace_period_days: 7
          activation_file: data/dq_rules_activation.txt

    In *flag-only* mode (before activation date) violations are recorded in
    ``dq_flags`` but the verdict is always ``"keep"``.  After the activation
    date the ``reject`` rules trigger a ``"reject"`` verdict.
    """

    def __init__(
        self,
        config: dict,
        activation_date: date | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        rules_cfg = config.get("rules", config)
        self._flag_rules: list[str] = list(rules_cfg.get("flag", []))
        self._reject_rules: list[str] = list(rules_cfg.get("reject", []))
        self._activation_date: date | None = activation_date
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

        unknown = set(self._flag_rules + self._reject_rules) - set(_RULE_FN)
        if unknown:
            logger.warning(
                "Unknown rule names in config",
                extra={"unknown": list(unknown)},
            )

        logger.info(
            "RulesEngine initialised",
            extra={
                "flag_rules": self._flag_rules,
                "reject_rules": self._reject_rules,
                "activation_date": str(activation_date),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, job_dict: dict) -> tuple[Verdict, dict]:
        """Classify a job dict.

        Returns ``(verdict, flags)`` where *flags* is a ``dict[str, bool]``
        containing triggered rule names.  When reject is not yet active the
        verdict is always ``"keep"`` regardless of rule violations.
        """
        flags: dict[str, bool] = {}

        # Evaluate flag rules
        for rule_name in self._flag_rules:
            fn = _RULE_FN.get(rule_name)
            if fn is None:
                continue
            if fn(job_dict):
                flags[rule_name] = True

        # Evaluate reject rules
        reject_triggered: list[str] = []
        for rule_name in self._reject_rules:
            fn = _RULE_FN.get(rule_name)
            if fn is None:
                continue
            if fn(job_dict):
                flags[rule_name] = True
                reject_triggered.append(rule_name)

        # Determine verdict
        if reject_triggered and self._activation_date is not None:
            now = self._now_fn()
            if is_reject_active(now, self._activation_date):
                logger.info(
                    "Job rejected by rules",
                    extra={"rules": reject_triggered},
                )
                return "reject", flags

        return "keep", flags

    @property
    def mode(self) -> str:
        """Human-readable mode string for health endpoint."""
        if self._activation_date is None:
            return "flag-only"
        now = self._now_fn()
        if is_reject_active(now, self._activation_date):
            return "flag+reject"
        return "flag-only"
