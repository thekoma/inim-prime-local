"""Tests for the multilingual zone-label device-class guesser."""

from __future__ import annotations

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.inim_prime.zone_guess import (
    _CLASS,
    SUPPORTED_LANGUAGES,
    _fold,
    guess_device_class,
    normalize_language,
)

# (language, label, expected category) — generated + verified by the
# multilingual vocabulary workflow (one set per supported language).
EXAMPLES: list[tuple[str, str, str]] = [
    ("it", "Fin. cucina PT", "window"),
    ("it", "Portafinestra salotto", "window"),
    ("it", "Porta ingresso principale", "door"),
    ("it", "Vol. soggiorno", "motion"),
    ("it", "Sabotaggio sirena esterna AS", "tamper"),
    ("it", "Rivelatore fumo mansarda", "smoke"),
    ("it", "Basculante box auto", "garage_door"),
    ("it", "Sonda allagamento lavanderia", "moisture"),
    ("it", "PIR ingresso scala", "motion"),
    ("it", "Contatto magnetico salone", "none"),
    ("en", "Front Door Reed", "door"),
    ("en", "Kitchen Window 1", "window"),
    ("en", "Hallway PIR", "motion"),
    ("en", "Siren Tamper AS", "tamper"),
    ("en", "Garage Overhead Door", "garage_door"),
    ("en", "Smoke Det. Landing", "smoke"),
    ("en", "Basement Water Leak", "moisture"),
    ("en", "Lounge Glass Break", "vibration"),
    ("en", "Lobby Keypad", "none"),
    ("en", "CO Det. Boiler Rm", "carbon_monoxide"),
    ("es", "Puerta entrada principal", "door"),
    ("es", "Vent. salon", "window"),
    ("es", "Volumetrico pasillo", "motion"),
    ("es", "Detector humo cocina", "smoke"),
    ("es", "Sabotaje sirena exterior", "tamper"),
    ("es", "Porton basculante garaje", "garage_door"),
    ("es", "Fuga gas caldera", "gas"),
    ("es", "Inundacion lavanderia", "moisture"),
    ("fr", "Detecteur fumee cuisine", "smoke"),
    ("fr", "IR Salon", "none"),
    ("fr", "Fenetr chambre parents", "window"),
    ("fr", "Porte entree principale", "door"),
    ("fr", "Autoprotection sirene exterieure", "tamper"),
    ("fr", "Detecteur gaz chaudiere", "gas"),
    ("fr", "Porte garage basculante", "garage_door"),
    ("fr", "Bris de glace baie vitree", "vibration"),
    ("de", "Fenster Wohnzimmer EG", "window"),
    ("de", "Haustuer", "door"),
    ("de", "Bewegung Flur OG", "motion"),
    ("de", "Sabotage Aussensirene", "tamper"),
    ("de", "Rauchmelder Schlafzimmer", "smoke"),
    ("de", "Garagentor", "garage_door"),
    ("de", "Wassermelder Keller", "moisture"),
    ("de", "Glasbruch Terrassentuer", "vibration"),
    ("pt", "Porta de entrada", "door"),
    ("pt", "Janela cozinha", "window"),
    ("pt", "Vol. sala", "motion"),
    ("pt", "Sabotagem central", "tamper"),
    ("pt", "Sirene exterior AS", "tamper"),
    ("pt", "Det. fumo garagem", "smoke"),
    ("pt", "Portão de garagem", "garage_door"),
    ("pt", "Fuga de gás cozinha", "gas"),
    ("nl", "Voordeur begane grond", "door"),
    ("nl", "MC schuifdeur tuin", "door"),
    ("nl", "Raam keuken", "window"),
    ("nl", "Beweging woonkamer PIR", "motion"),
    ("nl", "Sabotage centrale behuizing", "tamper"),
    ("nl", "Buitensirene AS", "tamper"),
    ("nl", "Rookmelder zolder", "smoke"),
    ("nl", "Garagepoort kanteldeur", "garage_door"),
    ("nl", "Glasbreuk veranda", "vibration"),
    ("nl", "Waterlek kelder CV", "moisture"),
    ("pl", "Czujka dymu kuchnia", "smoke"),
    ("pl", "PIR salon parter", "motion"),
    ("pl", "Drzwi wejsciowe", "door"),
    ("pl", "Okno sypialnia", "window"),
    ("pl", "Brama garazowa", "garage_door"),
    ("pl", "Sabotaz syreny zewn", "tamper"),
    ("pl", "Czujnik czadu kotlownia", "carbon_monoxide"),
    ("pl", "Zalanie pralnia", "moisture"),
    ("zh", "客厅红外", "motion"),
    ("zh", "厨房燃气泄漏", "gas"),
    ("zh", "主卧窗磁", "window"),
    ("zh", "入户防盗门", "door"),
    ("zh", "警号防拆", "tamper"),
    ("zh", "楼梯间烟感", "smoke"),
    ("zh", "厨房水浸探测", "moisture"),
    ("zh", "车库卷帘门", "garage_door"),
    ("zh", "地下室一氧化碳", "carbon_monoxide"),
    ("zh", "阳台双鉴幕帘", "motion"),
    ("ru", "Входная дверь 1 этаж", "door"),
    ("ru", "Окно кухня", "window"),
    ("ru", "Движ. гостиная PIR", "motion"),
    ("ru", "Дым кухня извещатель ИП-212", "smoke"),
    ("ru", "Тампер корпус ПКП", "tamper"),
    ("ru", "Протечка воды санузел", "moisture"),
    ("ru", "Ворота гараж секционные", "garage_door"),
    ("ru", "Утечка газ котельная", "gas"),
    ("ru", "Разбитие стекла зал", "vibration"),
    ("ru", "Сирена улица AS", "tamper"),
    ("ru", "Темп. подвал +5", "none"),
    ("ar", "كاشف دخان المطبخ", "smoke"),
    ("ar", "باب المدخل الرئيسي", "door"),
    ("ar", "نافذة غرفة النوم", "window"),
    ("ar", "كاشف حركة الصالة", "motion"),
    ("ar", "تخريب صفارة الإنذار الخارجية", "tamper"),
    ("ar", "كاشف تسرب غاز المطبخ", "gas"),
    ("ar", "باب الكراج", "garage_door"),
    ("ar", "كسر زجاج الواجهة", "vibration"),
    ("hi", "मुख्य दरवाजा", "door"),
    ("hi", "बैठक खिड़की", "window"),
    ("hi", "हॉल पीआईआर गति", "motion"),
    ("hi", "रसोई गैस सेंसर", "gas"),
    ("hi", "सीढ़ी धुआं डिटेक्टर", "smoke"),
    ("hi", "साइरन टैम्पर", "tamper"),
    ("hi", "गैराज शटर", "garage_door"),
    ("hi", "बेसमेंट पानी रिसाव", "moisture"),
    ("hi", "सीओ डिटेक्टर रसोई", "carbon_monoxide"),
    ("hi", "शोरूम कांच टूट", "vibration"),
]

# Real INIM PrimeX (fw 4.07) zone labels, in Italian.
REAL_PANEL: list[tuple[str, str]] = [
    ("Fin.Bagno PT", "window"),
    ("Finestra Taverna", "window"),
    ("Fin.Guardaroba", "window"),
    ("Finestra Camera", "window"),
    ("Volum.Camera", "motion"),
    ("Volum.Taverna", "motion"),
    ("Vol.Ext.Camera", "motion"),
    ("Volum.Ext Bagno", "motion"),
    ("AS Sirena Ext.", "tamper"),
    ("Tapp.Bagno PT", "window"),
    ("Tapp.Taverna", "window"),
    ("Tapp.Camera", "window"),
    ("Tapparella Sala", "window"),
    ("Finestra Sala", "window"),
    ("Lucernaio Sala", "window"),
    ("Volum.Sala", "motion"),
    ("Porta Ingresso", "door"),
    ("Vol.Ext.Sala", "motion"),
    ("Vol.Ext Lucern.", "motion"),
    ("Box", "garage_door"),
]


def _expected(name: str) -> BinarySensorDeviceClass | None:
    return None if name == "none" else _CLASS[name]


@pytest.mark.parametrize("language,label,expected", EXAMPLES)
def test_examples_classify(language: str, label: str, expected: str) -> None:
    """Every verified per-language example classifies as expected."""
    assert guess_device_class(label, language) is _expected(expected)


@pytest.mark.parametrize("label,expected", REAL_PANEL)
def test_real_panel_zones(label: str, expected: str) -> None:
    """Every real PrimeX zone label classifies correctly (language it)."""
    assert guess_device_class(label, "it") is _expected(expected)


def test_normalize_language() -> None:
    """Language codes normalize to a supported base, else English."""
    assert normalize_language(None) == "en"
    assert normalize_language("it") == "it"
    assert normalize_language("pt-BR") == "pt"
    assert normalize_language("en_GB") == "en"
    assert normalize_language("xx") == "en"  # unsupported -> English


def test_fold_strips_accents_and_case() -> None:
    """Folding lowercases and removes diacritics."""
    assert _fold("Portão GRANDÉ") == "portao grande"


def test_english_only_branch() -> None:
    """An English label uses the English vocabulary (no second vocab merge)."""
    assert guess_device_class("Kitchen Window", "en") is BinarySensorDeviceClass.WINDOW


def test_as_marker_wins_as_tamper() -> None:
    """A standalone INIM 'AS' marker maps to tamper, but 'gas' does not."""
    assert guess_device_class("Contatto AS", "it") is BinarySensorDeviceClass.TAMPER
    assert guess_device_class("Rivelatore gas cucina", "it") is BinarySensorDeviceClass.GAS


def test_no_match_returns_none() -> None:
    """A label with no known token returns None."""
    assert guess_device_class("Zona 7", "it") is None


def test_all_supported_languages_have_examples() -> None:
    """Sanity: the example matrix covers every supported language."""
    covered = {lang for lang, _, _ in EXAMPLES}
    assert covered == set(SUPPORTED_LANGUAGES)
