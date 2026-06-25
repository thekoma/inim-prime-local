"""Heuristic zone-label -> binary_sensor device-class guessing (multilingual).

Tokens per language (the 12 most relevant for INIM panels) are merged at match
time with English + a small universal/technical set, then matched as case- and
accent-insensitive substrings, tried most-specific-first so a hazard or
anti-sabotage zone never reads as a generic opening. Vocabularies are generated
from a verified multilingual word list; users can always override the device
class on the entity in Home Assistant.
"""

from __future__ import annotations

import re
import unicodedata

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

# Languages with a dedicated vocabulary (ISO-639-1). Anything else falls back to
# English (which is always merged in regardless).
SUPPORTED_LANGUAGES: tuple[str, ...] = ("it", "en", "es", "fr", "de", "pt", "nl", "pl", "zh", "ru", "ar", "hi")

# Categories tried most-specific-first; first matching category wins.
_ORDER: tuple[str, ...] = (
    "tamper",
    "smoke",
    "carbon_monoxide",
    "gas",
    "moisture",
    "vibration",
    "garage_door",
    "window",
    "door",
    "motion",
)

_CLASS: dict[str, BinarySensorDeviceClass] = {
    "tamper": BinarySensorDeviceClass.TAMPER,
    "smoke": BinarySensorDeviceClass.SMOKE,
    "carbon_monoxide": BinarySensorDeviceClass.CO,
    "gas": BinarySensorDeviceClass.GAS,
    "moisture": BinarySensorDeviceClass.MOISTURE,
    "vibration": BinarySensorDeviceClass.VIBRATION,
    "garage_door": BinarySensorDeviceClass.GARAGE_DOOR,
    "window": BinarySensorDeviceClass.WINDOW,
    "door": BinarySensorDeviceClass.DOOR,
    "motion": BinarySensorDeviceClass.MOTION,
}

# Language-independent technical tokens, always considered alongside the
# language + English vocabularies.
_UNIVERSAL: dict[str, tuple[str, ...]] = {
    "tamper": ("tamper",),
    "motion": ("pir", "radar", "volum"),
}

_LANG_VOCAB: dict[str, dict[str, tuple[str, ...]]] = {
    "it": {
        "tamper": ("sabotagg", "antisabotagg", "tamper", "manomission", "antistrappo", "autosorvegli", "supervis", "sirena", "sirene",),
        "smoke": ("fumo", "fumi", "incendio", "antincendio", "rivelatore fumo", "fire",),
        "carbon_monoxide": ("monossido", "ossido di carbon", "rivelatore co", "rivel.co", "rilevatore co",),
        "gas": ("gas", "metano", "gpl", "fuga gas", "rivelatore gas",),
        "moisture": ("allagam", "perdita acqua", "perdita d'acqua", "sonda acqua", "rivelatore acqua", "infiltrazion", "alluvion",),
        "vibration": ("vibrazione", "vibraz", "antishock", "antiscasso", "inerziale", "sismico", "rottura vetro", "rotturavetri", "antirottura",),
        "garage_door": ("garage", "box auto", "basculante", "saracinesca", "serranda", "autorimessa", "carraio", "box",),
        "window": ("finestr", "fin.", "tapparell", "avvolgibil", "persiana", "serramento", "lucernario", "portafinestr", "verand", "abbaino", "tapp", "lucernaio",),
        "door": ("porta", "portone", "cancello", "cancell", "portoncino", "porta servizio", "porta blindata", "varco",),
        "motion": ("volumetric", "vol.", "movimento", "movim", "infrarossi", "ir passivo", "pir", "presenza", "radar", "doppia tecnologia",),
    },
    "en": {
        "tamper": ("tamper", "sabotage", "antisab", "anti-sab", "enclosure", "as siren", "siren as", "supervis",),
        "smoke": ("smoke", "fire", "flame", "heat det", "smk",),
        "carbon_monoxide": ("carbon mon", "co det", "co alarm", "co sensor",),
        "gas": ("gas", "methane", "natural gas", "lpg", "propane", "combustible",),
        "moisture": ("water leak", "flood", "leak", "moisture", "water det", "damp", "sump",),
        "vibration": ("vibration", "shock", "glass break", "glassbreak", "glass-break", "seismic", "inertia",),
        "garage_door": ("garage", "overhead door", "sectional", "roller door", "carport", "roll-up", "rollup",),
        "window": ("window", "windw", "shutter", "skylight", "blind", "casement", "french window", "louvre", "louver",),
        "door": ("door", "entrance", "gate", "front door", "patio door", "service door",),
        "motion": ("motion", "pir", "movement", "volumetric", "presence", "radar", "dual-tech", "occupancy", "infrared",),
    },
    "es": {
        "tamper": ("tamper", "sabotaj", "antisabotaj", "antiarranq", "antiapertur", "autoproteccion", "autoprotec", "supervis", "sirena as", "sab sirena",),
        "smoke": ("humo", "incendi", "fuego", "detector humo", "det humo", "termovel", "detector termic",),
        "carbon_monoxide": ("monoxido", "monoxido carbono", "detector co", "det co", "co carbono",),
        "gas": ("gas", "metano", "glp", "butano", "propano", "gas natural", "fuga gas",),
        "moisture": ("inundacion", "fuga agua", "anegamien", "escape agua", "deteccion agua", "humedad", "sonda agua",),
        "vibration": ("vibrac", "sismic", "inercial", "rotura cristal", "rotura vidrio", "rotura crist", "impacto", "sacudid", "antichoq",),
        "garage_door": ("garaje", "garage", "cochera", "basculante", "seccional", "enrollable", "marquesin", "box",),
        "window": ("ventan", "ventanal", "vent", "persian", "claraboy", "tragaluz", "lucernari", "celosia", "balconer", "contravent",),
        "door": ("puerta", "porton entrada", "porton acceso", "cancela", "verja", "porton peatonal", "entrada principal", "acceso", "puerta servicio",),
        "motion": ("movimien", "volumetric", "infrarroj", "pir", "presencia", "radar", "doble tecnolog", "deteccion movim", "barrera infrarroj",),
    },
    "fr": {
        "tamper": ("autoprotec", "autosurveil", "sabotage", "antisabot", "antiarrach", "tamper", "as siren", "supervis sir", "protec sirene",),
        "smoke": ("fumee", "incendie", "detecteur fum", "detect fum", "dadf", "feu", "thermique",),
        "carbon_monoxide": ("monoxyde", "detecteur co", "detect co", "dac",),
        "gas": ("gaz", "methane", "propane", "butane", "fuite gaz", "detect gaz", "combustible",),
        "moisture": ("fuite eau", "degat eau", "inondation", "innondation", "detect eau", "flotteur", "humidite", "submersion",),
        "vibration": ("vibration", "choc", "bris vitre", "bris de glace", "sismique", "inertiel", "vitrage", "sismo",),
        "garage_door": ("porte garage", "sectionnelle", "basculante", "porte sect", "porte de gar", "garage",),
        "window": ("fenetr", "volet", "roulant", "store", "velux", "lucarne", "imposte", "oeil de boeuf",),
        "door": ("porte", "portail", "portillon", "entree", "porte service", "porte fenetr", "vantail", "issue secours",),
        "motion": ("mouvement", "detect mvt", "infrarouge", "volumet", "volumetrique", "presence", "radar", "double tech", "bi volume",),
    },
    "de": {
        "tamper": ("sabotage", "sabo", "deckel", "gehause", "manipula", "aufbruch", "abriss", "sirene", "selbstschutz",),
        "smoke": ("rauch", "brand", "feuer", "rauchmeld", "feuermeld", "thermomeld", "thermo",),
        "carbon_monoxide": ("kohlenmonoxid", "co-melder", "co melder", "co-warn", "co-sensor", "co warn",),
        "gas": ("gasmeld", "gaswarn", "gassensor", "methan", "erdgas", "fluessiggas", "propan", "butan",),
        "moisture": ("wasser", "leckage", "ueberschwemm", "uberschwemm", "feuchte",),
        "vibration": ("erschutter", "erschuetter", "vibration", "glasbruch", "seismik", "splitter",),
        "garage_door": ("garage", "sektionaltor", "rolltor", "carport",),
        "window": ("fenst", "rollladen", "rolladen", "jalousie", "dachfenster", "oberlicht", "lichtkuppel",),
        "door": ("tuer", "tuere", "eingang", "zugang", "gartentor", "hoftor",),
        "motion": ("bewegung", "bewegungsmeld", "bwm", "pir", "volumen", "raumuberwach", "raumueberwach", "praesenz", "prasenz", "radar", "dualtech",),
    },
    "pt": {
        "tamper": ("sabotag", "tamper", "antissabot", "anti-sabot", "violac", "arromba", "sirene", "sereia", "autoprotec", "supervis",),
        "smoke": ("fumo", "fumaca", "incendio", "fogo", "deteccao de fumo", "detetor de fumo", "detector de fumo",),
        "carbon_monoxide": ("monoxido", "monox", "monoxido de carbono", "detetor co", "detector co",),
        "gas": ("gas", "metano", "glp", "fuga de gas", "butano", "propano",),
        "moisture": ("agua", "inundac", "alagam", "fuga de agua", "vazament", "humidade", "umidade",),
        "vibration": ("vibrac", "choque", "quebra de vidro", "vidro", "sismic", "inercial", "impacto",),
        "garage_door": ("garagem", "portao de garagem", "basculante", "seccional", "portao seccional", "carport", "porta de garagem",),
        "window": ("janela", "estore", "persiana", "claraboia", "postigo", "vitral",),
        "door": ("porta", "portao", "entrada", "porta de entrada", "porta principal", "porta de servico", "cancela", "gradeam",),
        "motion": ("movimento", "volumetric", "volum", "vol.", "infraverm", "pir", "presenca", "radar", "dupla tecnolog",),
    },
    "nl": {
        "tamper": ("sabot", "antisabotage", "tamper", "behuizing", "deksel", "sirene", "buitensirene", "binnensirene", "flitser",),
        "smoke": ("rook", "brand", "vuur", "thermisch", "warmtemeld", "brandmeld",),
        "carbon_monoxide": ("koolmonoxide", "koolstofmonoxide", "koolmonox", "co-meld", "co-detect", "co melder",),
        "gas": ("gaslek", "gasmeld", "gasdetect", "aardgas", "methaan", "propaan", "lpg",),
        "moisture": ("water", "waterlek", "wateroverlast", "lekkage", "overstrom", "vocht", "vloeistof",),
        "vibration": ("trilling", "glasbreuk", "breuk", "schok", "seismisch", "inertie",),
        "garage_door": ("garage", "garagedeur", "garagepoort", "kanteldeur", "sectionaaldeur", "roldeur", "oprit", "carport",),
        "window": ("raam", "venster", "rolluik", "dakraam", "schuifraam", "kantelraam",),
        "door": ("deur", "voordeur", "achterdeur", "tuindeur", "schuifdeur", "toegang", "ingang", "hek", "poort",),
        "motion": ("beweging", "pir", "volumetrisch", "ruimtebewak", "aanwezigheid", "radar", "dualtech", "infrarood",),
    },
    "pl": {
        "tamper": ("sabota", "antysabota", "tamper", "obudow", "syren", "samoochr", "as syren", "ochrona syr",),
        "smoke": ("dym", "pozar", "pozaru", "ppoz", "czujka dym", "ostrzeg pozar",),
        "carbon_monoxide": ("czad", "tlenek wegla", "tlenku wegla", "czujnik czadu", "czujnik co",),
        "gas": ("gaz", "metan", "propan", "butan", "gaz ziemny", "wyciek gazu",),
        "moisture": ("zalan", "woda", "wody", "wyciek wody", "powodz", "wilg", "czujnik wody",),
        "vibration": ("wstrzas", "sejsmik", "sejsmiczn", "stluczen", "stluczenie szyb", "inercyjn", "drgan",),
        "garage_door": ("garaz", "garazu", "garazow", "brama garaz", "wrota", "podjazd", "sekcyjn",),
        "window": ("okno", "okna", "okien", "rolet", "okiennic", "swietlik", "balkonow", "zaluzj",),
        "door": ("drzwi", "wejsci", "furtk", "brama", "drzwi serwis",),
        "motion": ("ruch", "pir", "czujka ruch", "czujnik ruch", "pasywn", "dualn", "wolumetr", "obecnosc",),
    },
    "zh": {
        "tamper": ("\u9632\u62c6", "\u62c6\u52a8", "\u64ac\u52a8", "\u9632\u7834\u574f", "\u7834\u574f", "\u5916\u58f3", "\u8b66\u53f7", "\u9e23\u7b1b\u5668", "\u76d1\u7ba1",),
        "smoke": ("\u70df\u611f", "\u611f\u70df", "\u70df\u96fe", "\u706b\u8b66", "\u706b\u707e", "\u611f\u6e29", "\u6e29\u611f", "\u6d88\u9632",),
        "carbon_monoxide": ("\u4e00\u6c27\u5316\u78b3", "\u7164\u6c14\u4e2d\u6bd2", "co\u63a2\u6d4b",),
        "gas": ("\u71c3\u6c14", "\u53ef\u71c3\u6c14", "\u5929\u7136\u6c14", "\u7532\u70f7", "\u7164\u6c14", "\u6db2\u5316\u6c14", "\u6c14\u4f53\u6cc4\u6f0f",),
        "moisture": ("\u6c34\u6d78", "\u6f0f\u6c34", "\u6e17\u6c34", "\u6d78\u6c34", "\u79ef\u6c34", "\u6c34\u4f4d", "\u6ea2\u6c34",),
        "vibration": ("\u632f\u52a8", "\u9707\u52a8", "\u73bb\u7483\u7834\u788e", "\u788e\u73bb\u7483", "\u7834\u73bb", "\u51b2\u51fb", "\u5730\u9707",),
        "garage_door": ("\u8f66\u5e93\u95e8", "\u8f66\u5e93", "\u5377\u5e18\u95e8", "\u5377\u95f8\u95e8", "\u8f66\u623f",),
        "window": ("\u7a97\u6237", "\u7a97\u78c1", "\u767e\u53f6", "\u5377\u5e18", "\u5929\u7a97", "\u843d\u5730\u7a97", "\u98d8\u7a97",),
        "door": ("\u95e8\u78c1", "\u5927\u95e8", "\u5165\u6237\u95e8", "\u9632\u76d7\u95e8", "\u6237\u95e8", "\u6805\u95e8", "\u4fa7\u95e8", "\u540e\u95e8",),
        "motion": ("\u7ea2\u5916", "\u88ab\u52a8\u7ea2\u5916", "\u79fb\u52a8\u63a2\u6d4b", "\u79fb\u52a8", "\u4eba\u4f53\u611f\u5e94", "\u53cc\u9274", "\u5e55\u5e18", "\u96f7\u8fbe", "\u5fae\u6ce2",),
    },
    "ru": {
        "tamper": ("\u0442\u0430\u043c\u043f\u0435\u0440", "tamper", "\u0432\u0441\u043a\u0440\u044b\u0442", "\u0441\u0430\u0431\u043e\u0442\u0430\u0436", "\u0441\u0438\u0440\u0435\u043d", "siren", "\u0441\u0430\u043c\u043e\u043e\u0445\u0440\u0430\u043d", "\u0430\u0432\u0442\u043e\u043a\u043e\u043d\u0442\u0440\u043e\u043b",),
        "smoke": ("\u0434\u044b\u043c", "\u043f\u043e\u0436\u0430\u0440", "\u043f\u043e\u0436\u0430\u0440\u043d", "\u043e\u0433\u043d", "\u0438\u043f-", "\u0434\u0438\u043f", "\u0438\u0437\u0432\u0435\u0449\u0430\u0442\u0435\u043b",),
        "carbon_monoxide": ("\u0443\u0433\u0430\u0440\u043d", "\u043c\u043e\u043d\u043e\u043e\u043a\u0441\u0438\u0434", "co2", "\u0441\u0438\u0433\u043d\u0430\u043b co",),
        "gas": ("\u0433\u0430\u0437", "\u043c\u0435\u0442\u0430\u043d", "\u043f\u0440\u043e\u043f\u0430\u043d", "\u0441\u0436\u0438\u0436\u0435\u043d\u043d", "\u0443\u0442\u0435\u0447\u043a\u0430 \u0433\u0430\u0437",),
        "moisture": ("\u043f\u0440\u043e\u0442\u0435\u0447\u043a", "\u0437\u0430\u0442\u043e\u043f\u043b\u0435\u043d", "\u0437\u0430\u043b\u0438\u0432", "\u0432\u043e\u0434\u0430", "\u043f\u043e\u0442\u043e\u043f", "\u0432\u043b\u0430\u0433",),
        "vibration": ("\u0432\u0438\u0431\u0440\u0430\u0446", "\u0440\u0430\u0437\u0431\u0438\u0442", "\u0443\u0434\u0430\u0440", "\u0441\u0435\u0438\u0441\u043c", "\u0441\u0442\u0435\u043a\u043b", "\u0438\u043d\u0435\u0440\u0446", "\u0440\u0430\u0437\u0440\u0443\u0448",),
        "garage_door": ("\u0433\u0430\u0440\u0430\u0436", "\u0432\u043e\u0440\u043e\u0442", "\u0440\u043e\u043b\u043b\u0435\u0442", "\u0441\u0435\u043a\u0446\u0438\u043e\u043d\u043d", "\u043e\u0442\u043a\u0430\u0442\u043d",),
        "window": ("\u043e\u043a\u043d", "\u043e\u043a\u043e\u043d", "\u0448\u0442\u043e\u0440", "\u0436\u0430\u043b\u044e\u0437\u0438", "\u0441\u0442\u0430\u0432\u043d", "\u0444\u043e\u0440\u0442\u043e\u0447\u043a", "\u0431\u0430\u043b\u043a\u043e\u043d", "\u0440\u043e\u043b\u044c\u0441\u0442\u0430\u0432\u043d",),
        "door": ("\u0434\u0432\u0435\u0440\u044c", "\u0434\u0432\u0435\u0440", "\u0432\u0445\u043e\u0434", "\u043a\u0430\u043b\u0438\u0442\u043a", "\u0442\u0430\u043c\u0431\u0443\u0440",),
        "motion": ("\u0434\u0432\u0438\u0436\u0435\u043d", "\u0434\u0432\u0438\u0436", "\u043e\u0431\u044a\u0435\u043c", "pir", "\u043f\u0440\u0438\u0441\u0443\u0442\u0441\u0442\u0432", "\u0434\u0430\u0442\u0447\u0438\u043a \u0434\u0432\u0438\u0436", "\u0440\u0430\u0434\u0430\u0440",),
    },
    "ar": {
        "tamper": ("\u062a\u062e\u0631\u064a\u0628", "\u062a\u0627\u0645\u0628\u0631", "\u0639\u0628\u062b", "\u062d\u0645\u0627\u064a\u0629 \u0627\u0644\u062c\u0647\u0627\u0632", "\u0627\u0634\u0631\u0627\u0641", "\u0633\u064a\u0631\u064a\u0646", "\u0635\u0641\u0627\u0631\u0629", "\u0633\u0631\u064a\u0646\u0629", "\u0633\u0627\u0639\u0629",),
        "smoke": ("\u062f\u062e\u0627\u0646", "\u062d\u0631\u064a\u0642", "\u062d\u0631\u0627\u064a\u0642", "\u0643\u0627\u0634\u0641 \u062f\u062e\u0627\u0646", "\u0646\u0627\u0631",),
        "carbon_monoxide": ("\u0627\u0648\u0644 \u0627\u0643\u0633\u064a\u062f", "\u0627\u0648\u0644 \u0627\u0643\u0633\u064a\u062f \u0627\u0644\u0643\u0631\u0628\u0648\u0646", "\u0643\u0627\u0634\u0641 \u063a\u0627\u0632 \u0627\u0644\u0643\u0631\u0628\u0648\u0646", "\u0627\u062d\u0627\u062f\u064a \u0627\u0643\u0633\u064a\u062f",),
        "gas": ("\u063a\u0627\u0632", "\u062a\u0633\u0631\u0628 \u063a\u0627\u0632", "\u0643\u0627\u0634\u0641 \u063a\u0627\u0632", "\u063a\u0627\u0632 \u0637\u0628\u064a\u0639\u064a", "\u0645\u064a\u062b\u0627\u0646", "\u063a\u0627\u0632 \u0645\u0633\u0627\u0644", "\u0628\u0648\u062a\u0627\u062c\u0627\u0632", "\u0628\u0648\u062a\u0627\u063a\u0627\u0632",),
        "moisture": ("\u0645\u0627\u0621", "\u0645\u064a\u0627\u0647", "\u062a\u0633\u0631\u0628 \u0645\u0627\u0621", "\u062a\u0633\u0631\u0628 \u0645\u064a\u0627\u0647", "\u0641\u064a\u0636\u0627\u0646", "\u063a\u0645\u0631", "\u062a\u0633\u0631\u064a\u0628 \u0645\u064a\u0627\u0647",),
        "vibration": ("\u0627\u0647\u062a\u0632\u0627\u0632", "\u0643\u0633\u0631 \u0632\u062c\u0627\u062c", "\u0632\u062c\u0627\u062c", "\u0635\u062f\u0645\u0629", "\u0647\u0632\u0629", "\u0632\u0644\u0632\u0627\u0644", "\u0627\u0631\u062a\u062c\u0627\u062c",),
        "garage_door": ("\u0643\u0631\u0627\u062c", "\u062c\u0631\u0627\u062c", "\u0643\u0627\u0631\u0627\u062c", "\u0645\u0631\u0627\u0628", "\u0628\u0627\u0628 \u0627\u0644\u0643\u0631\u0627\u062c", "\u0628\u0627\u0628 \u0627\u0644\u062c\u0631\u0627\u062c", "\u0628\u0648\u0627\u0628\u0629 \u0627\u0644\u0643\u0631\u0627\u062c",),
        "window": ("\u0646\u0627\u0641\u0630", "\u0646\u0648\u0627\u0641\u0630", "\u0634\u0628\u0627\u0643", "\u0634\u0628\u0627\u0628\u064a\u0643", "\u0633\u062a\u0627\u0631\u0629 \u0645\u0639\u062f\u0646\u064a\u0629", "\u0631\u0648\u0634\u0646", "\u0645\u0646\u0648\u0631",),
        "door": ("\u0628\u0627\u0628", "\u0627\u0628\u0648\u0627\u0628", "\u0645\u062f\u062e\u0644", "\u0628\u0648\u0627\u0628\u0629", "\u0645\u062e\u0631\u062c", "\u0628\u0627\u0628 \u0631\u064a\u064a\u0633\u064a",),
        "motion": ("\u062d\u0631\u0643\u0629", "\u062d\u0631\u0643\u0647", "\u0643\u0627\u0634\u0641 \u062d\u0631\u0643\u0629", "\u062a\u0648\u0627\u062c\u062f", "\u062d\u0636\u0648\u0631", "\u062d\u062c\u0645\u064a", "\u0631\u0627\u062f\u0627\u0631", "\u0627\u0633\u062a\u0634\u0639\u0627\u0631 \u062d\u0631\u0643\u0629",),
    },
    "hi": {
        "tamper": ("\u091f\u0948\u092e\u092a\u0930", "\u091f\u0947\u092e\u092a\u0930", "\u0938\u092c\u094b\u091f\u093e\u091c", "\u091b\u0947\u0921\u091b\u093e\u0921", "\u0938\u093e\u0907\u0930\u0928", "\u0938\u093e\u092f\u0930\u0928", "\u0938\u0947\u0932\u092b \u092a\u0930\u094b\u091f\u0947\u0915\u0936\u0928", "\u090f\u090f\u0938",),
        "smoke": ("\u0927\u0941\u0906\u0902", "\u0927\u0941\u0902\u0906", "\u0905\u0917\u0928\u093f", "\u0905\u0917\u0932\u0917\u0940", "\u092b\u093e\u092f\u0930", "\u0938\u092e\u094b\u0915", "\u0906\u0917 \u0932\u0917",),
        "carbon_monoxide": ("\u0915\u093e\u0930\u092c\u0928 \u092e\u094b\u0928\u094b\u0911\u0915\u0938\u093e\u0907\u0921", "\u092e\u094b\u0928\u094b\u0911\u0915\u0938\u093e\u0907\u0921", "\u0915\u093e\u0930\u092c\u0928 \u092e\u094b\u0928\u094b", "\u0938\u0940\u0913 \u0917\u0948\u0938", "\u0938\u0940\u0913 \u0921\u093f\u091f\u0947\u0915\u091f\u0930", "\u0938\u0940\u0913 \u0938\u0947\u0902\u0938\u0930",),
        "gas": ("\u0917\u0948\u0938", "\u090f\u0932\u092a\u0940\u091c\u0940", "\u0930\u0938\u094b\u0908 \u0917\u0948\u0938", "\u092e\u0940\u0925\u0947\u0928", "\u0938\u093f\u0932\u0947\u0902\u0921\u0930", "\u092a\u0930\u093e\u0915\u0943\u0924\u093f\u0915 \u0917\u0948\u0938",),
        "moisture": ("\u092a\u093e\u0928\u0940", "\u092a\u093e\u0928\u0940 \u0930\u093f\u0938\u093e\u0935", "\u091c\u0932 \u0930\u093f\u0938\u093e\u0935", "\u0930\u093f\u0938\u093e\u0935", "\u092c\u093e\u0922", "\u091c\u0932\u092d\u0930\u093e\u0935", "\u0932\u0940\u0915\u0947\u091c", "\u0935\u093e\u091f\u0930 \u0932\u0940\u0915",),
        "vibration": ("\u091d\u091f\u0915\u093e", "\u0915\u093e\u0902\u091a \u091f\u0942\u091f", "\u0936\u0940\u0936\u093e \u091f\u0942\u091f", "\u0917\u0932\u093e\u0938 \u092c\u0930\u0947\u0915", "\u092d\u0942\u0915\u0902\u092a\u0940\u092f", "\u0915\u0902\u092a\u0928 \u0921\u093f\u091f\u0947\u0915\u091f\u0930",),
        "garage_door": ("\u0917\u0948\u0930\u093e\u091c", "\u0917\u0930\u093e\u091c", "\u0917\u0948\u0930\u0947\u091c", "\u0936\u091f\u0930", "\u0930\u094b\u0932\u093f\u0902\u0917 \u0936\u091f\u0930", "\u0915\u093e\u0930 \u092a\u094b\u0930\u091a", "\u0935\u093e\u0939\u0928 \u0926\u0935\u093e\u0930",),
        "window": ("\u0916\u093f\u0921\u0915\u0940", "\u0916\u093f\u0921\u0915\u093f\u092f\u093e\u0902", "\u0930\u094b\u0936\u0928\u0926\u093e\u0928", "\u091d\u0930\u094b\u0916\u093e", "\u092c\u0932\u093e\u0907\u0902\u0921",),
        "door": ("\u0926\u0930\u0935\u093e\u091c", "\u0926\u0935\u093e\u0930", "\u0917\u0947\u091f", "\u092b\u093e\u091f\u0915", "\u092e\u0941\u0916\u092f \u0926\u0935\u093e\u0930", "\u092a\u0930\u0935\u0947\u0936 \u0926\u0935\u093e\u0930",),
        "motion": ("\u0917\u0924\u093f", "\u092e\u0942\u0935\u092e\u0947\u0902\u091f", "\u0939\u0932\u091a\u0932", "\u092a\u0940\u0906\u0908\u0906\u0930", "\u0935\u0949\u0932\u092f\u0942\u092e\u0947\u091f\u0930\u093f\u0915", "\u0930\u0921\u093e\u0930", "\u0909\u092a\u0938\u0925\u093f\u0924\u093f", "\u092e\u094b\u0936\u0928",),
    },
}

def _fold(text: str) -> str:
    """Lowercase and strip diacritics for accent-insensitive matching."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_language(language: str | None) -> str:
    """Map an HA language code to a supported base code, else 'en'."""
    if not language:
        return "en"
    base = language.lower().replace("_", "-").split("-", 1)[0]
    return base if base in _LANG_VOCAB else "en"


def guess_device_class(
    label: str, language: str | None = None
) -> BinarySensorDeviceClass | None:
    """Guess a zone's device class from its label, for at-a-glance icons.

    ``language`` selects the vocabulary (English is always merged in too);
    unknown/None falls back to English. Returns None when nothing matches.
    """
    folded = _fold(label)
    # INIM "AS" (autosorveglianza) is a standalone marker for a siren/tamper
    # supervision line; match it as a whole word so it can't hit "gas"/"casa".
    if re.search(r"\bas\b", folded):
        return BinarySensorDeviceClass.TAMPER

    lang = normalize_language(language)
    vocabs = (_LANG_VOCAB["en"],) if lang == "en" else (_LANG_VOCAB["en"], _LANG_VOCAB[lang])
    for category in _ORDER:
        tokens: tuple[str, ...] = _UNIVERSAL.get(category, ())
        for vocab in vocabs:
            tokens += vocab.get(category, ())
        if any(token in folded for token in tokens):
            return _CLASS[category]
    return None
