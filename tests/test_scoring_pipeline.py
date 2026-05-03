import asyncio

import app.scoring.keyword  # noqa: F401

from app.scoring.pipeline import ScoringPipeline
from app.models.job import NormalizedJob, ScoredJob
from app.scoring.types import ScoringProfile


def _make_job(title="Agile Coach", desc="coaching agile teams"):
    return NormalizedJob(
        title=title, url="http://test.com/1", source="test", description=desc
    )


def _make_profile(archetypes=None, kw_pos=None, kw_neg=None):
    return ScoringProfile(
        id="test",
        archetypes=archetypes or {"coach": 1.0},
        keywords_positive=kw_pos or ["Agile"],
        keywords_negative=kw_neg or [],
    )


def _make_pipeline():
    return ScoringPipeline(
        {
            "stages": [
                {
                    "scorer_id": "keyword",
                    "stage": 1,
                    "enabled": True,
                    "weights": {
                        "archetype_match": 30,
                        "keyword_positive": 25,
                        "seniority": 15,
                        "remote_bonus": 10,
                        "noise_penalty": -20,
                    },
                }
            ],
            "store_threshold": 30,
        }
    )


def test_agile_coach_high_score():
    pipe = _make_pipeline()
    profile = _make_profile(archetypes={"coach": 1.0}, kw_pos=["Agile"])
    job = _make_job("Agile Coach", "coaching agile teams remote")
    results = asyncio.run(pipe.run_stage1([job], profile))
    assert results[0].score_stage_1 >= 30


def test_zero_archetype_low_score():
    pipe = _make_pipeline()
    profile = _make_profile(archetypes={"coach": 0.0}, kw_pos=[])
    job = _make_job("Agile Coach", "coaching agile teams")
    results = asyncio.run(pipe.run_stage1([job], profile))
    assert results[0].score_stage_1 < 30


def test_negative_keyword_penalty():
    pipe = _make_pipeline()
    profile = _make_profile(
        archetypes={"coach": 1.0}, kw_pos=["Agile"], kw_neg=["Junior"]
    )
    job = _make_job("Junior Agile Coach", "junior coaching role")
    results = asyncio.run(pipe.run_stage1([job], profile))
    # Compare with same job but no negative keywords
    profile2 = _make_profile(
        archetypes={"coach": 1.0}, kw_pos=["Agile"], kw_neg=[]
    )
    results2 = asyncio.run(pipe.run_stage1([job], profile2))
    assert results[0].score_stage_1 <= results2[0].score_stage_1


def test_filter_by_threshold():
    pipe = _make_pipeline()
    jobs = [
        ScoredJob(
            title="High", url="http://1", source="t", score_stage_1=50, profile_id="t"
        ),
        ScoredJob(
            title="Low", url="http://2", source="t", score_stage_1=10, profile_id="t"
        ),
        ScoredJob(
            title="Edge", url="http://3", source="t", score_stage_1=30, profile_id="t"
        ),
    ]
    kept, discarded = pipe.filter_by_threshold(jobs)
    assert len(kept) == 2
    assert discarded == 1
    assert all(j.score_stage_1 >= 30 for j in kept)


def test_config_driven_disabled_stage():
    pipe = ScoringPipeline(
        {
            "stages": [
                {"scorer_id": "keyword", "stage": 1, "enabled": False},
            ],
            "store_threshold": 30,
        }
    )
    assert len(pipe.stages) == 0
