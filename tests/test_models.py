from custom_components.inim_prime.client import FAULT_FLAG_KEYS, Fault, Version


def test_version_firmware_and_model():
    version = Version(raw="5.00 PX020", service=False)
    assert version.firmware == "5.00"
    assert version.model_name == "PX020"


def test_version_model_without_digits_is_the_family():
    assert Version(raw="4.07 PRIME", service=False).model_name == "PrimeX"
    assert Version(raw="4.07", service=False).model_name == "PrimeX"


def test_fault_flags_default_all_false():
    fault = Fault(vcc=24.3, has_faults=False)
    assert fault.flags == dict.fromkeys(FAULT_FLAG_KEYS, False)
    assert fault.raw == ""
