"""Авто/ПІБ карток Petroline: розбір «Авто | ПІБ», перейменування водія, прив'язка «Автомобільної» картки.

HTTP підмінено: реальні API не викликаються. Формати відповідей — як на живому API (2026-11).
"""

from unittest import mock

import pytest

from fuel.providers import petroline_cards as pl
from fuel.providers.petroline_api import PetrolineClient, PetrolineError
from fuel.providers.petroline_cards import PetrolineCards, format_label, label_fields, parse_label


@pytest.fixture(autouse=True)
def petroline_env(monkeypatch):
    monkeypatch.delenv("PETROLINE_V2_LOGIN", raising=False)
    monkeypatch.delenv("PETROLINE_V2_PASSWORD", raising=False)
    monkeypatch.setenv("PETROLINE_LOGIN", "test-login")
    monkeypatch.setenv("PETROLINE_PASS", "test-password")


def response(payload, status=200, text=None):
    resp = mock.Mock()
    resp.ok = status < 400
    resp.status_code = status
    if payload is None:
        resp.json.side_effect = ValueError("empty")
        resp.text = text or ""
    else:
        resp.json.return_value = payload
        resp.text = str(payload)
    return resp


class FakeApi:
    """Сесія Petroline: відповідає на POST/PUT/DELETE за шляхом і записує виклики."""

    def __init__(self, *, keys=(), persons=(), create_person=41143, fail_keys_update=False):
        self.keys, self.persons = list(keys), list(persons)
        self.create_person = create_person
        self.fail_keys_update = fail_keys_update
        self.calls: list[tuple[str, str, dict | None]] = []
        self.headers: dict = {}

    def post(self, url, json=None, timeout=None):
        path = url.split(".ua", 1)[1]
        self.calls.append(("post", path, json))
        if path == "/api/Auth/token":
            return response({"token": "jwt"})
        if path == "/api/Keys/list":
            leaf = json["where"]["operands"][0]
            return response([r for r in self.keys if str(r.get(leaf["field"])) == str(leaf["value"])])
        if path == "/api/Persons/list":  # сторінками, без фільтра
            return response(self.persons[json["skip"] : json["skip"] + json["take"]])
        if path == "/api/Persons/create":
            return response(self.create_person)
        raise AssertionError(path)

    def put(self, url, json=None, timeout=None):
        path = url.split(".ua", 1)[1]
        self.calls.append(("put", path, json))
        if path == "/api/Keys/update" and self.fail_keys_update:
            return response({"value": None, "status": 400, "errors": ["bad"]})
        if path == "/api/Keys/update":
            return response({"value": str(json["id"]), "status": 200, "returnObject": None, "errors": None})
        return response(None)  # Persons/update — порожнє тіло

    def delete(self, url, json=None, timeout=None):
        self.calls.append(("delete", url.split(".ua", 1)[1], json))
        return response(None)

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "post" or p == "/api/Persons/create"]


def cards_with(api: FakeApi) -> PetrolineCards:
    return PetrolineCards(client=PetrolineClient(session=api))


KEY = {
    "id": 6001,
    "number": "0000AAAA01",
    "extNumber": "010001",
    "groupId": 10,
    "personId": 501,
    "transportId": None,
    "status": 3,
    "typeId": 1,
    "barrelId": None,
    "pin": None,
    "comment": None,
    "secondCardMode": 0,
    "secondCardId": None,
}
VEHICLE_KEY = {**KEY, "typeId": 5, "personId": None, "transportId": 3001}
PERSON = {
    "id": 501,
    "name": "Тестовий Т ",
    "groupId": 10,
    "email": None,
    "phone": None,
    "fuels": [1, 2],
    "status": 3,
}


# --- розбір і форматування --------------------------------------------------------------------


@pytest.mark.parametrize(
    "vehicle, driver, expected",
    [
        ("AA0000AA", "Іваненко Іван", "AA0000AA | Іваненко Іван"),
        ("AA0000AA", "", "AA0000AA"),
        ("", " Іваненко  Іван ", "Іваненко Іван"),
        ("", "", ""),
    ],
)
def test_format_label(vehicle, driver, expected):
    assert format_label(vehicle, driver) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("AA0000AA | Іваненко Іван  ", ("AA0000AA", "Іваненко Іван")),
        ("AA0000AA_Тестовий", ("AA0000AA_Тестовий", "")),  # старі імена без «|» («Авто_ПІБ») — усе в Авто
        (" | Іваненко Іван", ("", "Іваненко Іван")),
        ("", ("", "")),
    ],
)
def test_parse_label(text, expected):
    assert parse_label(text) == expected


@pytest.mark.parametrize(
    "card_type, person, expected",
    [
        (1, "501", pl.EDIT_RENAME),
        (1, "", pl.EDIT_LINK),
        (3, "501", pl.EDIT_DRIVER),
        (5, "", pl.EDIT_LINK),
        (5, "501", pl.EDIT_LINK),
        (2, "501", pl.EDIT_NONE),
        (None, "", pl.EDIT_NONE),  # тип ще не оновлено з Petroline — нічого не пишемо
    ],
)
def test_edit_mode(card_type, person, expected):
    assert pl.edit_mode(card_type, person) == expected


def test_label_fields_by_card():
    assert label_fields(1, "501", "AA0000AA_Тестовий") == ("AA0000AA_Тестовий", "")
    assert label_fields(3, "501", "Технічний Пролив") == ("", "Технічний Пролив")
    assert label_fields(5, "", "FORD AI17-51KI") == ("FORD AI17-51KI", "")


def test_normalize_vehicle_card_uses_transport():
    rec = {**VEHICLE_KEY, "transportName": "FORD", "transportNumber": "AI17-51KI"}

    row = PetrolineCards._normalize(rec)

    assert (row["vehicle"], row["driver_name"], row["label"]) == ("FORD AI17-51KI", "", "FORD AI17-51KI")
    assert (row["card_type"], row["transport_id"], row["person_id"]) == (5, "3001", "")


def test_normalize_personal_without_bar_goes_to_vehicle():
    row = PetrolineCards._normalize({**KEY, "personName": "Тестовий Т "})

    assert (row["vehicle"], row["driver_name"], row["label"]) == ("Тестовий Т", "", "Тестовий Т")


# --- перейменування водія ---------------------------------------------------------------------


def test_personal_card_renames_existing_person():
    api = FakeApi(keys=[KEY], persons=[PERSON])

    result = cards_with(api).update_info(
        "0000AAAA01", vehicle="AA0000AA", driver_name="Тестовий Т", expected=(1, "501")
    )

    assert api.writes() == [
        (
            "put",
            "/api/Persons/update",
            {
                "id": 501,
                "groupId": 10,
                "email": None,
                "phone": None,
                "fuels": [1, 2],
                "status": 3,
                "surname": "AA0000AA | Тестовий Т",
                "name": "",
                "middlename": "",
            },
        )
    ]
    assert result == pl.InfoResult(1, "501", "", "AA0000AA | Тестовий Т", action="renamed")


def test_tech_card_writes_only_driver():
    api = FakeApi(keys=[{**KEY, "typeId": 3}], persons=[PERSON])

    result = cards_with(api).update_info("0000AAAA01", vehicle="ignored", driver_name="Технічний Пролив")

    assert api.writes()[0][2]["surname"] == "Технічний Пролив"
    assert result.label == "Технічний Пролив"


def test_rename_refused_when_card_changed_since_sync():
    api = FakeApi(keys=[{**KEY, "personId": 777}], persons=[PERSON])

    with pytest.raises(PetrolineError, match="змінилась після останнього оновлення"):
        cards_with(api).update_info("0000AAAA01", driver_name="Новий", expected=(1, "501"))
    assert api.writes() == []


def test_rename_refused_when_name_taken_by_other_person():
    other = {**PERSON, "id": 900, "name": "aa0000aa | тестовий т"}
    api = FakeApi(keys=[KEY], persons=[PERSON, other])

    with pytest.raises(PetrolineError, match="вже є інший водій"):
        cards_with(api).update_info("0000AAAA01", vehicle="AA0000AA", driver_name="Тестовий Т")
    assert api.writes() == []


def test_unknown_type_is_refused():
    api = FakeApi(keys=[{**KEY, "typeId": 2}], persons=[PERSON])

    with pytest.raises(PetrolineError, match="Переміщення"):
        cards_with(api).update_info("0000AAAA01", driver_name="X")
    assert api.writes() == []


def test_empty_name_is_refused():
    api = FakeApi(keys=[KEY], persons=[PERSON])

    with pytest.raises(PetrolineError, match="порожнім"):
        cards_with(api).update_info("0000AAAA01", vehicle="", driver_name="")
    assert api.writes() == []


# --- «Автомобільна» -> «Персональна» ---------------------------------------------------------


def test_vehicle_card_links_to_existing_person_with_same_name():
    owner = {**PERSON, "id": 700, "name": "AA0000AA | Тестовий Т "}
    api = FakeApi(keys=[VEHICLE_KEY], persons=[PERSON, owner])

    result = cards_with(api).update_info("0000AAAA01", vehicle="AA0000AA", driver_name="тестовий т")

    ((method, path, key),) = api.writes()  # водія не створюємо
    assert (method, path) == ("put", "/api/Keys/update")
    assert (key["typeId"], key["personId"], key["transportId"], key["id"]) == (1, 700, None, 6001)
    assert result == pl.InfoResult(1, "700", "", "AA0000AA | тестовий т", action="linked")


def test_vehicle_card_refuses_inactive_or_ambiguous_person():
    inactive = {**PERSON, "id": 700, "name": "AA0000AA | Тестовий Т", "status": 5}
    with pytest.raises(PetrolineError, match="неактивний"):
        cards_with(FakeApi(keys=[VEHICLE_KEY], persons=[inactive])).update_info(
            "0000AAAA01", vehicle="AA0000AA", driver_name="Тестовий Т"
        )

    twins = [{**inactive, "status": 3}, {**inactive, "id": 701, "status": 3}]
    api = FakeApi(keys=[VEHICLE_KEY], persons=twins)
    with pytest.raises(PetrolineError, match="кілька водіїв"):
        cards_with(api).update_info("0000AAAA01", vehicle="AA0000AA", driver_name="Тестовий Т")
    assert api.writes() == []


def test_vehicle_card_without_such_person_creates_one():
    api = FakeApi(keys=[VEHICLE_KEY], persons=[PERSON], create_person=41143)

    result = cards_with(api).update_info("0000AAAA01", vehicle="AA0000AA", driver_name="Іваненко Іван")

    (m1, p1, create), (m2, p2, key) = api.writes()
    assert (m1, p1) == ("post", "/api/Persons/create")
    assert create["surname"] == "AA0000AA | Іваненко Іван"
    assert (create["groupId"], create["fuels"], create["status"]) == (10, [1], 3)
    assert (m2, p2) == ("put", "/api/Keys/update")
    assert (key["typeId"], key["personId"], key["transportId"]) == (1, 41143, None)
    assert result == pl.InfoResult(1, "41143", "", "AA0000AA | Іваненко Іван", action="created")


def test_failed_link_deletes_created_person():
    api = FakeApi(keys=[VEHICLE_KEY], fail_keys_update=True)

    with pytest.raises(PetrolineError, match="400"):
        cards_with(api).update_info("0000AAAA01", vehicle="AA0000AA", driver_name="")

    assert api.writes()[-1][:2] == ("delete", "/api/Persons/delete/41143")
