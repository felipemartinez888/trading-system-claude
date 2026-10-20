from __future__ import annotations

from datetime import date

from tsclaude.cli import build_parser


def test_data_commands_parse() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "data",
            "import",
            "--market",
            "usdm",
            "--symbols",
            "BTCUSDT,ETHUSDT",
            "--from",
            "2022-01-01",
            "--to",
            "2026-09-30",
            "--kinds",
            "bars_1m,funding",
        ]
    )
    assert args.func.__name__ == "_cmd_import"
    assert (args.date_from, args.date_to) == (date(2022, 1, 1), date(2026, 9, 30))
    uni = parser.parse_args(["data", "universe", "--snapshot", "2026-10-19"])
    assert uni.func.__name__ == "_cmd_universe"
    assert uni.rule == "configs/universe_rule.yaml"
    ver = parser.parse_args(["data", "verify", "--all"])
    assert ver.all
    assert ver.dataset_id is None
