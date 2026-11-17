"""Scoring monotonicity and ranking tests."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from tests.support import FIXED_NOW, make_salary, make_vacancy
from vacancy_radar.models import Query
from vacancy_radar.scoring import (
    COMPONENT_WEIGHTS,
    RECENCY_HALF_LIFE_DAYS,
    age_days,
    keyword_fit,
    rank_vacancies,
    recency_fit,
    score_vacancy,
)


class ScoringMonotonicityTests(unittest.TestCase):
    def test_better_salary_scores_higher(self) -> None:
        lower = make_vacancy(
            vacancy_id="low",
            salary=make_salary(100_000, None, "RUB", gross=False),
        )
        higher = replace(
            lower,
            id="high",
            salary=make_salary(150_000, None, "RUB", gross=False),
        )
        query = Query(min_salary=100_000)

        self.assertGreater(
            score_vacancy(higher, query, FIXED_NOW).total,
            score_vacancy(lower, query, FIXED_NOW).total,
        )

    def test_salary_scores_are_monotonic_across_cases(self) -> None:
        amounts = [50_000, 75_000, 100_000, 150_000, 300_000]
        scores = [
            score_vacancy(
                make_vacancy(
                    salary=make_salary(amount, None, "RUB", gross=False)
                ),
                Query(),
                FIXED_NOW,
            ).total
            for amount in amounts
        ]
        for earlier, later in zip(scores, scores[1:]):
            self.assertLessEqual(earlier, later)

    def test_more_keywords_score_higher(self) -> None:
        both = make_vacancy(
            vacancy_id="both",
            title="Python FastAPI developer",
            snippet="SQL and Docker",
        )
        one = replace(
            both,
            id="one",
            title="Python developer",
            snippet="FastAPI services",
        )
        none = replace(
            both,
            id="none",
            title="Backend developer",
            snippet="Java and Spring",
        )
        query = Query(keywords=("python", "fastapi"))

        scores = [
            score_vacancy(vacancy, query, FIXED_NOW).total
            for vacancy in (both, one, none)
        ]
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[1], scores[2])

    def test_title_keyword_weighs_more_than_snippet(self) -> None:
        title_hit = make_vacancy(
            vacancy_id="title",
            title="Python developer",
            snippet="SQL",
        )
        snippet_hit = replace(
            title_hit,
            id="snippet",
            title="Backend developer",
            snippet="Python services",
        )
        query = Query(keywords=("python",))

        title_score, title_note = keyword_fit(title_hit, query.keywords)
        snippet_score, _ = keyword_fit(snippet_hit, query.keywords)
        self.assertGreater(title_score, snippet_score)
        self.assertIn("title", title_note)
        self.assertGreater(
            score_vacancy(title_hit, query, FIXED_NOW).total,
            score_vacancy(snippet_hit, query, FIXED_NOW).total,
        )

    def test_fresher_vacancies_score_higher(self) -> None:
        fresh = make_vacancy(
            vacancy_id="fresh",
            published_at=FIXED_NOW - timedelta(hours=1),
        )
        week_old = replace(fresh, id="week", published_at=FIXED_NOW - timedelta(days=7))
        month_old = replace(fresh, id="month", published_at=FIXED_NOW - timedelta(days=30))
        query = Query()

        fresh_score = score_vacancy(fresh, query, FIXED_NOW).total
        week_score = score_vacancy(week_old, query, FIXED_NOW).total
        month_score = score_vacancy(month_old, query, FIXED_NOW).total
        self.assertGreater(fresh_score, week_score)
        self.assertGreater(week_score, month_score)

    def test_recency_half_life_is_seven_days(self) -> None:
        published = FIXED_NOW - timedelta(days=RECENCY_HALF_LIFE_DAYS)
        fit, note = recency_fit(published, FIXED_NOW)
        self.assertAlmostEqual(fit, 0.5)
        self.assertIn("7-day half-life", note)

    def test_future_publication_date_is_clamped(self) -> None:
        future = FIXED_NOW + timedelta(days=2)
        fit, _ = recency_fit(future, FIXED_NOW)
        self.assertEqual(fit, 1.0)
        self.assertEqual(age_days(future, FIXED_NOW), 0.0)

    def test_remote_bonus_only_applies_when_requested(self) -> None:
        office = make_vacancy(vacancy_id="office", remote=False)
        remote = replace(office, id="remote", remote=True)

        without_preference = Query(remote=False)
        self.assertEqual(
            score_vacancy(office, without_preference, FIXED_NOW).total,
            score_vacancy(remote, without_preference, FIXED_NOW).total,
        )
        self.assertNotIn(
            "remote",
            score_vacancy(office, without_preference, FIXED_NOW).weights,
        )

        with_preference = Query(remote=True)
        remote_score = score_vacancy(remote, with_preference, FIXED_NOW)
        office_score = score_vacancy(office, with_preference, FIXED_NOW)
        self.assertGreater(remote_score.total, office_score.total)
        self.assertEqual(remote_score.components["remote"], 1.0)
        self.assertEqual(office_score.components["remote"], 0.0)

    def test_named_employer_scores_above_anonymous(self) -> None:
        named = make_vacancy(vacancy_id="named")
        anonymous = replace(named, id="anonymous", employer=None)
        query = Query()

        self.assertGreater(
            score_vacancy(named, query, FIXED_NOW).total,
            score_vacancy(anonymous, query, FIXED_NOW).total,
        )
        self.assertIn(
            "anonymous",
            score_vacancy(anonymous, query, FIXED_NOW).notes["employer"],
        )

    def test_currency_mismatch_is_penalised(self) -> None:
        rub = make_vacancy(
            vacancy_id="rub",
            salary=make_salary(150_000, None, "RUB", gross=False),
        )
        usd = replace(
            rub,
            id="usd",
            salary=make_salary(150_000, None, "USD", gross=False),
        )
        query = Query(currency="RUB")

        self.assertGreater(
            score_vacancy(rub, query, FIXED_NOW).total,
            score_vacancy(usd, query, FIXED_NOW).total,
        )

    def test_unknown_salary_is_not_scored_as_zero_salary(self) -> None:
        unknown = replace(make_vacancy(), salary=None)
        zero = replace(
            unknown,
            salary=make_salary(0, None, "RUB", gross=False),
        )
        query = Query()

        unknown_score = score_vacancy(unknown, query, FIXED_NOW)
        zero_score = score_vacancy(zero, query, FIXED_NOW)
        self.assertGreater(unknown_score.components["salary"], 0.0)
        self.assertGreater(
            unknown_score.components["salary"],
            zero_score.components["salary"],
        )
        self.assertIn("unknown", unknown_score.notes["salary"])


class ScoreBreakdownTests(unittest.TestCase):
    def test_breakdown_is_complete_and_consistent(self) -> None:
        score = score_vacancy(
            make_vacancy(),
            Query(remote=True, keywords=("python", "fastapi")),
            FIXED_NOW,
        )

        self.assertEqual(
            set(score.components),
            {"salary", "keywords", "recency", "remote", "employer"},
        )
        self.assertEqual(set(score.weights), set(score.components))
        self.assertEqual(set(score.notes), set(score.components))
        for name, component in score.components.items():
            self.assertGreaterEqual(component, 0.0, name)
            self.assertLessEqual(component, 1.0, name)
            self.assertGreaterEqual(score.weights[name], 0.0, name)
        self.assertAlmostEqual(sum(score.weights.values()), 1.0)
        self.assertAlmostEqual(sum(score.contributions.values()), score.total)
        self.assertAlmostEqual(
            score.total,
            sum(
                score.components[name] * score.weights[name]
                for name in score.components
            )
            * 100,
        )

    def test_no_remote_renormalises_weights(self) -> None:
        score = score_vacancy(make_vacancy(), Query(remote=False), FIXED_NOW)
        self.assertAlmostEqual(sum(score.weights.values()), 0.90)
        self.assertNotIn("remote", score.components)
        self.assertAlmostEqual(
            score.weights["salary"],
            COMPONENT_WEIGHTS["salary"],
        )

    def test_no_keywords_is_neutral_not_zero(self) -> None:
        score = score_vacancy(make_vacancy(), Query(keywords=()), FIXED_NOW)
        self.assertEqual(score.components["keywords"], 0.5)
        self.assertIn("neutral", score.notes["keywords"])

    def test_deterministic_for_fixed_now(self) -> None:
        vacancy = make_vacancy()
        query = Query(remote=True, keywords=("python",))
        first = score_vacancy(vacancy, query, FIXED_NOW)
        second = score_vacancy(vacancy, query, FIXED_NOW)
        self.assertEqual(first, second)


class RankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.high = make_vacancy(
            vacancy_id="high",
            title="Senior Python developer",
            salary=make_salary(300_000, None, "RUB", gross=False),
            published_at=FIXED_NOW - timedelta(hours=2),
        )
        self.mid = replace(
            self.high,
            id="mid",
            title="Python developer",
            salary=make_salary(150_000, None, "RUB", gross=False),
            published_at=FIXED_NOW - timedelta(days=5),
        )
        self.low = replace(
            self.high,
            id="low",
            title="Junior developer",
            salary=make_salary(80_000, None, "RUB", gross=False),
            published_at=FIXED_NOW - timedelta(days=40),
        )

    def test_ranked_by_score_descending(self) -> None:
        ranked = rank_vacancies(
            [self.low, self.mid, self.high],
            Query(min_salary=50_000, top=10),
            FIXED_NOW,
        )
        self.assertEqual([vacancy.id for vacancy, _ in ranked], ["high", "mid", "low"])
        scores = [score.total for _, score in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_limits_results(self) -> None:
        ranked = rank_vacancies(
            [self.low, self.mid, self.high],
            Query(top=2),
            FIXED_NOW,
        )
        self.assertEqual(len(ranked), 2)

    def test_remote_only_filters(self) -> None:
        remote = replace(self.high, id="remote", remote=True)
        office = replace(self.mid, id="office", remote=False)
        ranked = rank_vacancies(
            [remote, office],
            Query(remote_only=True, top=10),
            FIXED_NOW,
        )
        self.assertEqual([vacancy.id for vacancy, _ in ranked], ["remote"])

    def test_salary_floor_drops_only_verifiable_below_floor(self) -> None:
        below = replace(
            self.low,
            id="below",
            salary=make_salary(90_000, None, "RUB", gross=False),
        )
        unknown = replace(self.low, id="unknown", salary=None)
        mismatched = replace(
            self.low,
            id="mismatch",
            salary=make_salary(90_000, None, "USD", gross=False),
        )
        ranked = rank_vacancies(
            [below, unknown, mismatched],
            Query(min_salary=100_000, top=10),
            FIXED_NOW,
        )
        ids = {vacancy.id for vacancy, _ in ranked}
        self.assertNotIn("below", ids)
        self.assertEqual(ids, {"unknown", "mismatch"})

    def test_tie_break_prefers_newer_then_id(self) -> None:
        older = replace(
            self.high,
            id="older",
            published_at=FIXED_NOW - timedelta(days=1),
        )
        newer = replace(
            self.high,
            id="newer",
            published_at=FIXED_NOW - timedelta(hours=1),
        )
        same_time = replace(
            newer,
            id="same",
            published_at=newer.published_at,
        )
        ranked = rank_vacancies(
            [same_time, older, newer],
            Query(top=10),
            FIXED_NOW,
        )
        ids = [vacancy.id for vacancy, _ in ranked]
        # Newer wins on recency; an exact tie on timestamp falls back to id.
        self.assertLess(ids.index("newer"), ids.index("older"))
        self.assertLess(ids.index("same"), ids.index("older"))
        self.assertLess(ids.index("newer"), ids.index("same"))


if __name__ == "__main__":
    unittest.main()
