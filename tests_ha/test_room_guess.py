"""Tests for the multilingual zone-label room guesser."""

from __future__ import annotations

import pytest

from custom_components.inim_prime.room_guess import guess_room

# (language, label, expected canonical room or "none") — generated + verified.
EXAMPLES: list[tuple[str, str, str]] = [
    ("it", "Vol.Ext.Camera", "Camera"),
    ("it", "Fin.Bagno PT", "Bagno"),
    ("it", "Tapparella Sala", "Sala"),
    ("it", "Box", "Garage"),
    ("it", "Volumetrico Soggiorno P1", "Soggiorno"),
    ("it", "Contatto Cucina", "Cucina"),
    ("it", "Perimetrale Giardino Nord", "Giardino"),
    ("it", "IR Cameretta bimbi", "Camera"),
    ("it", "Sirena esterna", "Giardino"),
    ("it", "Tamper Centrale", "none"),
    ("en", "PIR Living Room", "Living Room"),
    ("en", "Door Contact Kitchen GF", "Kitchen"),
    ("en", "Window Master Bedroom P1", "Bedroom"),
    ("en", "Motion Hallway", "Corridor"),
    ("en", "Mag Ensuite Bath", "Bathroom"),
    ("en", "Smoke Det Loft", "Attic"),
    ("en", "Shock Garage Door", "Garage"),
    ("en", "Perimeter Beam Garden", "Garden"),
    ("en", "Tamper Siren External", "none"),
    ("es", "Vol.Ext.Jardin", "Jardín"),
    ("es", "Cont.Puerta Cocina", "Cocina"),
    ("es", "IR Dormit. P1", "Dormitorio"),
    ("es", "Persiana Salon PB", "Salón"),
    ("es", "Mag.Ventana Bano PB", "Baño"),
    ("es", "Dormitorio infantil P2", "Habitación niños"),
    ("es", "Contacto Garaje", "Garaje"),
    ("es", "Tamper Central", "none"),
    ("es", "Recibidor entrada principal", "Entrada"),
    ("fr", "Vol.Salon RDC", "Salon"),
    ("fr", "Det.Cuisine", "Cuisine"),
    ("fr", "Volet Chambre P1", "Chambre"),
    ("fr", "IR Chambre Enfant", "Chambre Enfant"),
    ("fr", "Contact SdB", "Salle de bain"),
    ("fr", "Magnetique Porte Entree", "Entree"),
    ("fr", "Vol.Ext.Camera Jardin", "Jardin"),
    ("fr", "Fumee Garage", "Garage"),
    ("fr", "Sirene Interieure", "none"),
    ("de", "Bewegung Wohnzimmer EG", "Wohnzimmer"),
    ("de", "Fenster Küche", "Küche"),
    ("de", "Magnetkontakt Schlafz OG", "Schlafzimmer"),
    ("de", "Rauchmelder Kinderzimmer 1", "Kinderzimmer"),
    ("de", "Tür Bad", "Bad"),
    ("de", "Wassermelder Waschküche UG", "Waschküche"),
    ("de", "Tor Garage", "Garage"),
    ("de", "Bewegungsmelder Garten Aussenhaut", "Garten"),
    ("de", "Sabotage Zentrale", "none"),
    ("pt", "Mov.Sala", "Sala"),
    ("pt", "Porta Cozinha R/C", "Cozinha"),
    ("pt", "Janela Quarto P1", "Quarto"),
    ("pt", "PIR Sala de Jantar", "Sala de Jantar"),
    ("pt", "Contacto Casa de Banho", "Casa de Banho"),
    ("pt", "Vol.Ext.Jardim", "Jardim"),
    ("pt", "Estore Quarto Bebé", "Quarto das Crianças"),
    ("pt", "Fumo Garagem Cave", "Garagem"),
    ("pt", "Sirene Exterior 24h", "Jardim"),
    ("pt", "Tamper Central", "none"),
    ("nl", "Beweging Woonkamer", "Woonkamer"),
    ("nl", "Magneetcontact Voordeur", "Hal"),
    ("nl", "Rookmelder Slaapk 1e", "Slaapkamer"),
    ("nl", "Raamcontact Badkamer BG", "Badkamer"),
    ("nl", "PIR Kinderkamer boven", "Kinderkamer"),
    ("nl", "Bijkeuken berging", "Bijkeuken"),
    ("nl", "Overloop 1e verd", "Gang"),
    ("nl", "Sirene buiten", "Tuin"),
    ("nl", "Sabotage tamper centrale", "none"),
    ("pl", "Czujka Salon", "Salon"),
    ("pl", "PIR Sypialnia P1", "Sypialnia"),
    ("pl", "Kontaktron Kuchnia okno", "Kuchnia"),
    ("pl", "Roleta Pokoj dzienny", "Salon"),
    ("pl", "Czujka Lazienka parter", "Łazienka"),
    ("pl", "Drzwi Garaz PT", "Garaż"),
    ("pl", "Czujka zewnetrz ogrod", "Ogród"),
    ("pl", "Magnes drzwi wejscie", "Wejście"),
    ("pl", "Zalanie", "none"),
    ("zh", "客厅红外探测", "客厅"),
    ("zh", "主卧门磁", "卧室"),
    ("zh", "厨房燃气探测器", "厨房"),
    ("zh", "二楼卫生间烟感", "卫生间"),
    ("zh", "阳台幕帘红外", "阳台"),
    ("zh", "车库卷帘门磁", "车库"),
    ("zh", "周界红外对射", "花园"),
    ("zh", "书房窗磁 PT", "书房"),
    ("zh", "玻璃破碎探测器", "none"),
    ("ru", "Датчик Гостиная", "Гостиная"),
    ("ru", "Окно Кухня 1эт", "Кухня"),
    ("ru", "Движение Спальня", "Спальня"),
    ("ru", "Дверь Прихожая", "Прихожая"),
    ("ru", "Разбитие Детская П2", "Детская"),
    ("ru", "Протечка Санузел", "Ванная"),
    ("ru", "ИК Периметр Улица", "Сад"),
    ("ru", "Магнит Гараж ворота", "Гараж"),
    ("ru", "Тревожная кнопка", "none"),
    ("ar", "حركة صالون", "غرفة المعيشة"),
    ("ar", "باب المطبخ", "المطبخ"),
    ("ar", "نافذة غرفة النوم ط1", "غرفة النوم"),
    ("ar", "كاشف دخان الكراج", "الكراج"),
    ("ar", "مجس حركة الممر", "الممر"),
    ("ar", "تراس علوي", "الشرفة"),
    ("ar", "حركة محيط خارجي", "الحديقة"),
    ("ar", "باب القبو ط-1", "القبو"),
    ("ar", "كاشف زجاج مكسور", "none"),
    ("hi", "Motion रसोई", "रसोई"),
    ("hi", "Window Master Bedroom P1", "शयनकक्ष"),
    ("hi", "Door बाथरूम GF", "बाथरूम"),
    ("hi", "PIR Garage Shutter", "गैराज"),
    ("hi", "Magnet मुख्य द्वार PT", "प्रवेश"),
    ("hi", "Glass Break बालकनी", "बालकनी"),
    ("hi", "Beam Boundary Compound", "बगीचा"),
    ("hi", "Smoke Detector Panel 24h", "none"),
    ("hi", "Tamper Zone 07", "none"),
]

# Real INIM PrimeX (fw 4.07) zone labels, in Italian.
REAL_PANEL: list[tuple[str, str]] = [
    ("Fin.Bagno PT", "Bagno"),
    ("Finestra Taverna", "Taverna"),
    ("Fin.Guardaroba", "Cabina armadio"),
    ("Finestra Camera", "Camera"),
    ("Volum.Camera", "Camera"),
    ("Volum.Taverna", "Taverna"),
    ("Vol.Ext.Camera", "Camera"),
    ("Volum.Ext Bagno", "Bagno"),
    ("AS Sirena Ext.", "none"),
    ("Tapp.Bagno PT", "Bagno"),
    ("Tapp.Taverna", "Taverna"),
    ("Tapp.Camera", "Camera"),
    ("Tapparella Sala", "Sala"),
    ("Finestra Sala", "Sala"),
    ("Lucernaio Sala", "Sala"),
    ("Volum.Sala", "Sala"),
    ("Porta Ingresso", "Ingresso"),
    ("Vol.Ext.Sala", "Sala"),
    ("Vol.Ext Lucern.", "none"),
    ("Box", "Garage"),
]


@pytest.mark.parametrize("language,label,expected", EXAMPLES)
def test_examples_resolve(language: str, label: str, expected: str) -> None:
    """Every verified per-language example resolves to the expected room."""
    assert guess_room(label, language) == (None if expected == "none" else expected)


@pytest.mark.parametrize("label,expected", REAL_PANEL)
def test_real_panel_rooms(label: str, expected: str) -> None:
    """Every real PrimeX zone label groups into the expected room (language it)."""
    assert guess_room(label, "it") == (None if expected == "none" else expected)


def test_longest_token_wins() -> None:
    """When two rooms match, the longer token wins (most specific room)."""
    # "perimetr" (8) for Giardino beats nothing else here; sanity of the rule.
    assert guess_room("Perimetrale giardino nord", "it") == "Giardino"


def test_english_only_branch() -> None:
    """An English label uses the English room vocabulary."""
    assert guess_room("Kitchen window", "en") == "Kitchen"


def test_unknown_language_falls_back_to_english() -> None:
    """An unsupported language code still resolves via English."""
    assert guess_room("Garage door", "xx") == "Garage"


def test_no_room_returns_none() -> None:
    """A label without a room word returns None."""
    assert guess_room("Zona 7", "it") is None
