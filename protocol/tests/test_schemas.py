from pyvised_protocol import PROTOCOL_VERSION
from pyvised_protocol.schemas import HealthInfo, LibraryInfo


def test_health_info_round_trip():
    health = HealthInfo(
        protocol_version=PROTOCOL_VERSION,
        backend_version="0.1.0",
        openmc_version="0.16.0",
        python_version="3.12.3",
        cpu_count=8,
        capabilities=["selftest"],
        libraries=[LibraryInfo(id="builtin", name="ENDF/B-VII.1", path="/x.xml", builtin=True)],
    )
    again = HealthInfo.model_validate_json(health.model_dump_json())
    assert again == health
    assert again.libraries[0].neutron == 0
