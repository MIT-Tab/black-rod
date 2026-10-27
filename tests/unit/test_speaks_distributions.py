from decimal import Decimal

from core.utils.speaks_distributions import new_round_speaks_samples, summarize_round_speaks


def test_empty_round_distribution():
    distribution = summarize_round_speaks(new_round_speaks_samples())
    assert distribution["sample_size"] == 0
    assert distribution["overall"]["mean"] is None
    assert distribution["correspondence"] == []
    assert distribution["chart"] is None


def test_single_score_distribution_is_defined_and_reaches_100_percent():
    samples = new_round_speaks_samples()
    samples["scores"] = [Decimal(30)]
    samples["by_record"][None] = [Decimal(30)]
    distribution = summarize_round_speaks(samples)
    assert distribution["overall"]["p25"] == 30
    assert distribution["overall"]["mean"] == 30
    assert distribution["correspondence"] == [
        {"score": Decimal(30), "sample_size": 1, "cumulative_count": 1, "percentile": Decimal(100)},
    ]
    assert distribution["chart"]["points"][0]["percentile"] == 100
