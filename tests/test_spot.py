import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from parking.accounts import AuthService
from parking.spot import SpotStore


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def logins(tmp_path):
    svc = AuthService(str(tmp_path / "auth.db"), "s" * 32)
    for name in ("one", "two"):                       # users 1 and 2 exist, as they do in the real file
        svc.create_user(name, "a long enough password")
    return svc


@pytest.fixture
def spots(tmp_path, logins):
    return SpotStore(str(tmp_path / "auth.db"), clock=Clock())


def test_nothing_saved_at_first(spots):
    assert spots.get(1) is None


def test_save_and_read_back(spots):
    got = spots.set(1, 45.5255, -73.576, accuracy=12.5, street="rue Boyer", note="  near the corner  ")
    assert got["lat"] == 45.5255 and got["lon"] == -73.576 and got["accuracy_m"] == 12.5
    assert got["street"] == "rue Boyer" and got["note"] == "near the corner"
    assert got["parked_at"] == "2027-01-15T08:00:00Z"
    assert spots.get(1) == got


def test_a_new_spot_replaces_the_old_one(spots):
    spots.set(1, 45.5, -73.5, note="level 2")
    spots.set(1, 45.6, -73.6)
    got = spots.get(1)
    assert (got["lat"], got["lon"], got["note"]) == (45.6, -73.6, "")


def test_each_login_has_its_own_spot(spots):
    spots.set(1, 45.5, -73.5)
    spots.set(2, 45.7, -73.7)
    assert spots.get(1)["lat"] == 45.5 and spots.get(2)["lat"] == 45.7
    assert spots.clear(1) is True
    assert spots.get(1) is None and spots.get(2) is not None
    assert spots.clear(1) is False


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181), (float("nan"), 0), (0, float("inf"))])
def test_impossible_positions_are_refused(spots, lat, lon):
    with pytest.raises(ValueError):
        spots.set(1, lat, lon)
    assert spots.get(1) is None


def test_notes_are_trimmed_cleaned_and_can_be_edited(spots):
    spots.set(1, 45.5, -73.5)
    assert spots.set_note(1, "x" * 500)["note"] == "x" * 120
    assert spots.set_note(1, "a\x00b\tc")["note"] == "abc"
    assert spots.set_note(2, "no spot for this login") is None


def test_an_absurd_accuracy_is_dropped(spots):
    assert spots.set(1, 45.5, -73.5, accuracy=float("nan"))["accuracy_m"] is None
    assert spots.set(1, 45.5, -73.5, accuracy=-5)["accuracy_m"] is None


def test_deleting_a_login_deletes_its_spot_and_a_new_login_never_inherits_it(spots, logins):
    spots.set(2, 45.5, -73.5, note="sam's car")
    assert logins.delete_user("two") is True
    assert spots.get(2) is None                                     # gone with the login
    logins.create_user("three", "another long password")            # SQLite reuses id 2 for the newest login
    assert logins.users()[-1]["id"] == 2
    assert spots.get(2) is None                                     # the new login does not see the old car


def test_a_spot_for_a_login_that_does_not_exist_is_refused(spots):
    with pytest.raises(ValueError):
        spots.set(99, 45.5, -73.5)
