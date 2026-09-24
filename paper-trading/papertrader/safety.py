"""Live trading is deliberately locked.

There is no live broker in this codebase. The paper phase has to be finished and the
strategies that pass the promotion criteria (see `report`) have to be picked before
anyone builds a live execution adapter. Until then, any config that asks for something
other than paper mode is refused.
"""


class LiveTradingLocked(RuntimeError):
    pass


def assert_paper_mode(config: dict):
    mode = str(config.get("mode", "paper")).lower()
    if mode != "paper":
        raise LiveTradingLocked(
            f"mode '{mode}' requested, but live trading is LOCKED. Only 'paper' is allowed until the "
            "paper-trading phase is finalized and a live broker adapter has been built and reviewed."
        )
