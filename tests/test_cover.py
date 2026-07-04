"""Tests for cover platform."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.cover import ATTR_POSITION
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_BLIND_ID,
    CONF_CLOSE_TIME,
    CONF_COMMAND_DEVICE_ID,
    CONF_COMMAND_ENUM,
    CONF_DEVICE_ENUM,
    CONF_DEVICE_ID,
    CONF_OPEN_TIME,
    CONF_SECONDARY_STATUS_IDENTITIES,
    CONF_SERIAL_PORT,
    CONF_STATUS_DEVICE_ID,
    CONF_STATUS_ENUM,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
    STATUS_IDENTITY_SOURCE_UNKNOWN,
    SUBENTRY_TYPE_BLIND,
)
from custom_components.schellenberg_usb.cover import (
    DEFAULT_TRAVEL_TIME,
    SchellenbergCover,
    async_setup_entry,
)

TEST_BLIND_ID = "11111111-1111-4111-8111-111111111111"


def _async_mock(value: Any) -> AsyncMock:
    """Cast helper for AsyncMock assertions."""
    return cast(AsyncMock, value)


def _magic_mock(value: Any) -> MagicMock:
    """Cast helper for MagicMock assertions."""
    return cast(MagicMock, value)


@pytest.fixture
def mock_api(hass: HomeAssistant) -> SchellenbergUsbApi:
    """Create a mock API."""
    api_mock = MagicMock(spec=SchellenbergUsbApi)
    api_mock.hass = hass
    api_mock.is_connected = True
    api_mock.device_version = "RFTU_V20"
    api_mock.control_blind = AsyncMock()
    api_mock.register_entity = MagicMock()
    return cast(SchellenbergUsbApi, api_mock)


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> ConfigEntry:
    """Create a mock config entry with subentries."""
    # Create a real subentry dict instead of MagicMock to avoid serialization issues
    subentry = MagicMock()
    subentry.subentry_id = "sub1"
    subentry.subentry_type = SUBENTRY_TYPE_BLIND
    subentry.data = {
        CONF_BLIND_ID: TEST_BLIND_ID,
        "device_id": "ABC123",
        "device_enum": "01",
        "device_name": "Test Cover",
    }
    subentry.title = "Test Cover"  # Real string, not mock

    entry = ConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Schellenberg USB",
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        options={},
        entry_id="test_entry_cover",
        state=ConfigEntryState.NOT_LOADED,
        minor_version=1,
        source="test",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    # Mock the subentries property
    entry.subentries = MappingProxyType({"sub1": subentry})  # type: ignore[misc]
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.mark.asyncio
async def test_async_setup_entry_creates_covers(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test that setup entry creates cover entities."""
    mock_config_entry.runtime_data = mock_api

    # Mock device registry
    dev_reg = dr.async_get(hass)

    # Create a hub device
    dev_reg.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, mock_config_entry.entry_id)},
        name="Schellenberg USB Stick",
        manufacturer="Schellenberg",
    )

    mock_add_entities = MagicMock()

    await async_setup_entry(hass, mock_config_entry, mock_add_entities)

    mock_add_entities.assert_called_once()
    entities = mock_add_entities.call_args[0][0]
    assert len(entities) == 1
    assert isinstance(entities[0], SchellenbergCover)
    assert entities[0]._device_id == "ABC123"
    assert entities[0]._device_enum == "01"


@pytest.mark.asyncio
async def test_setup_migrates_legacy_entity_registry_unique_id(
    hass: HomeAssistant,
    mock_config_entry: ConfigEntry,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test registry migration preserves the existing entity ID."""
    mock_config_entry.runtime_data = mock_api
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "cover",
        DOMAIN,
        "schellenberg_ABC123",
        config_entry=mock_config_entry,
        config_subentry_id="sub1",
        suggested_object_id="extension_0",
    )
    add_entities = MagicMock()

    await async_setup_entry(hass, mock_config_entry, add_entities)

    new_unique_id = f"{DOMAIN}_blind_{TEST_BLIND_ID}"
    assert registry.async_get_entity_id("cover", DOMAIN, new_unique_id) == (
        legacy.entity_id
    )
    assert registry.async_get_entity_id("cover", DOMAIN, "schellenberg_ABC123") is None
    assert add_entities.call_args.args[0][0].unique_id == new_unique_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_enum", "status_enum"),
    [("23", "08"), ("08", "0D"), ("0D", "08")],
)
async def test_setup_restores_manual_cover_from_persisted_subentry(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
    command_enum: str,
    status_enum: str,
) -> None:
    """Test a stored manual blind is recreated during platform setup."""
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Schellenberg USB",
        data={CONF_SERIAL_PORT: "/dev/ttyUSB0"},
        options={},
        source="user",
        unique_id="/dev/ttyUSB0",
        discovery_keys=MappingProxyType({}),
        subentries_data=[
            {
                "subentry_id": "manual_blind",
                "subentry_type": SUBENTRY_TYPE_BLIND,
                "title": "Sitting room door",
                "unique_id": "F2B8D5",
                "data": {
                    CONF_BLIND_ID: TEST_BLIND_ID,
                    CONF_DEVICE_ID: "F2B8D5",
                    CONF_DEVICE_ENUM: "23",
                    CONF_COMMAND_DEVICE_ID: "F2B8D5",
                    CONF_COMMAND_ENUM: command_enum,
                    CONF_STATUS_DEVICE_ID: "3720B8",
                    CONF_STATUS_ENUM: status_enum,
                    CONF_SECONDARY_STATUS_IDENTITIES: [
                        {"device_id": "F2B8D5", "enum": "23"}
                    ],
                    CONF_OPEN_TIME: 25.06,
                    CONF_CLOSE_TIME: 23.05,
                },
            }
        ],
    )
    entry.runtime_data = mock_api
    hass.config_entries._entries[entry.entry_id] = entry
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    _magic_mock(mock_api.register_entity).assert_called_once_with(
        "3720B8",
        status_enum,
        "Sitting room door",
        command_device_id="F2B8D5",
        command_enum=command_enum,
        secondary_status_identities=(("F2B8D5", "23"),),
    )

    add_entities.assert_called_once()
    assert add_entities.call_args.kwargs == {"config_subentry_id": "manual_blind"}
    cover = add_entities.call_args.args[0][0]
    assert isinstance(cover, SchellenbergCover)
    assert cover.name is None
    assert cover._device_name == "Sitting room door"
    assert cover.unique_id == f"{DOMAIN}_blind_{TEST_BLIND_ID}"
    assert cover._command_enum == command_enum
    assert cover._status_device_id == "3720B8"
    assert cover._status_enum == status_enum
    assert cover._secondary_status_identities == (("F2B8D5", "23"),)
    assert cover._travel_time_open == 25.06
    assert cover._travel_time_close == 23.05


@pytest.mark.asyncio
async def test_cover_initialization(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover initialization."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
        device_data=None,
        config_entry_id="test_entry",
        blind_id=TEST_BLIND_ID,
    )

    assert cover._device_id == "ABC123"
    assert cover._device_enum == "01"
    assert cover.unique_id == f"{DOMAIN}_blind_{TEST_BLIND_ID}"
    assert cover.name is None
    assert cover._device_name == "Test Cover"
    assert cover._attr_current_cover_position is None
    assert cover._travel_time_open == DEFAULT_TRAVEL_TIME
    assert cover._travel_time_close == DEFAULT_TRAVEL_TIME


def test_cover_unique_id_is_stable_after_rename(
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test a friendly-name change cannot change registry identity."""
    original = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Extension 0",
        blind_id=TEST_BLIND_ID,
    )
    renamed = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Garden blind",
        blind_id=TEST_BLIND_ID,
    )
    other_blind = SchellenbergCover(
        api=mock_api,
        device_id="DEF456",
        device_enum="02",
        device_name="Other blind",
        blind_id="22222222-2222-4222-8222-222222222222",
    )

    assert original.unique_id == renamed.unique_id
    assert original._device_name != renamed._device_name
    assert other_blind.unique_id != original.unique_id


@pytest.mark.asyncio
async def test_cover_initialization_with_calibration(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover initialization with calibration data."""
    device_data = {
        CONF_OPEN_TIME: 25.0,
        CONF_CLOSE_TIME: 23.0,
    }

    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
        device_data=device_data,
        config_entry_id="test_entry",
    )

    assert cover._travel_time_open == 25.0
    assert cover._travel_time_close == 23.0


@pytest.mark.asyncio
async def test_cover_availability(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover availability based on API connection."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )

    assert cover.available is True

    cast(Any, mock_api).is_connected = False
    assert cover.available is False


@pytest.mark.asyncio
async def test_cover_icon_states(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover icon changes based on state."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )

    # Closed state
    cover._attr_is_closed = True
    assert cover.icon == "mdi:window-shutter"

    # Open state
    cover._attr_is_closed = False
    assert cover.icon == "mdi:window-shutter-open"

    # Opening state
    cover._attr_is_opening = True
    assert cover.icon == "mdi:arrow-up-box"

    # Closing state
    cover._attr_is_opening = False
    cover._attr_is_closing = True
    assert cover.icon == "mdi:arrow-down-box"


@pytest.mark.asyncio
async def test_cover_async_open_cover(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test opening the cover."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 0

    with patch.object(cover, "_start_position_tracking"):
        with patch.object(cover, "async_write_ha_state"):
            await cover.async_open_cover()

    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    _async_mock(mock_api.control_blind).assert_called_once_with(
        "01", "01", device_id="ABC123"
    )


@pytest.mark.asyncio
async def test_cover_uses_split_identity_and_inverted_direction(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test commands use command identity while status direction is inverted."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="STABLE1",
        device_enum="23",
        device_name="Sitting room",
        command_device_id="F2B8D5",
        status_device_id="3720B8",
        status_enum="08",
        invert_direction=True,
    )
    cover.hass = hass
    cover._attr_current_cover_position = 0

    with (
        patch.object(cover, "_start_position_tracking"),
        patch.object(cover, "async_write_ha_state"),
    ):
        await cover.async_open_cover()
        cover._handle_event(EVENT_STARTED_MOVING_DOWN)

    _async_mock(mock_api.control_blind).assert_awaited_once_with(
        "23", "02", device_id="F2B8D5"
    )
    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["C1", "C2", "C3"])
async def test_unknown_secondary_commands_do_not_change_position_tracking(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
    event: str,
) -> None:
    """Test opaque secondary commands leave movement and position unchanged."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="F2B8D5",
        device_enum="10",
        device_name="Sitting room",
        status_device_id="3720B8",
        status_enum="08",
        secondary_status_identities=(("F2B8D5", "23"),),
    )
    cover.hass = hass
    cover._attr_current_cover_position = 50

    with (
        patch.object(cover, "_start_position_tracking") as start_tracking,
        patch.object(cover, "async_write_ha_state"),
    ):
        cover._handle_event(event)

    start_tracking.assert_not_called()
    assert cover._attr_current_cover_position == 50
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._move_start_time is None
    _magic_mock(mock_api.record_position_update).assert_not_called()


@pytest.mark.asyncio
async def test_cover_async_close_cover(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test closing the cover."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 100

    with patch.object(cover, "_start_position_tracking"):
        with patch.object(cover, "async_write_ha_state"):
            await cover.async_close_cover()

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is True
    _async_mock(mock_api.control_blind).assert_called_once_with(
        "01", "02", device_id="ABC123"
    )


@pytest.mark.asyncio
async def test_cover_async_stop_cover(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test stopping the cover."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 50

    with patch.object(cover, "_stop_position_tracking"):
        with patch.object(cover, "async_write_ha_state"):
            await cover.async_stop_cover()

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    _async_mock(mock_api.control_blind).assert_called_once_with(
        "01", "00", device_id="ABC123"
    )


@pytest.mark.asyncio
async def test_cover_set_position_open(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test setting cover to a higher position (opening)."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 20

    with patch.object(cover, "async_open_cover", new_callable=AsyncMock) as mock_open:
        await cover.async_set_cover_position(**{ATTR_POSITION: 80})

    assert cover._target_position == 80
    mock_open.assert_called_once()


@pytest.mark.asyncio
async def test_cover_set_position_close(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test setting cover to a lower position (closing)."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 80

    with patch.object(cover, "async_close_cover", new_callable=AsyncMock) as mock_close:
        await cover.async_set_cover_position(**{ATTR_POSITION: 20})

    assert cover._target_position == 20
    mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_cover_set_position_same(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test setting cover to same position does nothing."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 50

    with patch.object(cover, "async_open_cover", new_callable=AsyncMock) as mock_open:
        with patch.object(
            cover, "async_close_cover", new_callable=AsyncMock
        ) as mock_close:
            await cover.async_set_cover_position(**{ATTR_POSITION: 50})

    mock_open.assert_not_called()
    mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_cover_restore_position(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover restores position from previous state."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass

    last_state = State("cover.test_cover", "open", {"current_position": 75})

    with patch.object(cover, "async_get_last_state", return_value=last_state):
        with patch("custom_components.schellenberg_usb.cover.async_dispatcher_connect"):
            with patch.object(cover, "async_write_ha_state"):
                await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 75
    assert cover._attr_is_closed is False


@pytest.mark.asyncio
async def test_cover_restore_closed(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover restores closed state."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass

    last_state = State("cover.test_cover", "closed", {"current_position": 0})

    with patch.object(cover, "async_get_last_state", return_value=last_state):
        with patch("custom_components.schellenberg_usb.cover.async_dispatcher_connect"):
            with patch.object(cover, "async_write_ha_state"):
                await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


@pytest.mark.asyncio
async def test_cover_no_previous_state(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover defaults to closed when no previous state."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass

    with patch.object(cover, "async_get_last_state", return_value=None):
        with patch("custom_components.schellenberg_usb.cover.async_dispatcher_connect"):
            with patch.object(cover, "async_write_ha_state"):
                await cover.async_added_to_hass()

    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


@pytest.mark.asyncio
async def test_cover_handle_started_moving_up(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test handling started moving up event."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 0

    with patch.object(cover, "_start_position_tracking"):
        with patch.object(cover, "async_write_ha_state"):
            cover._handle_event(EVENT_STARTED_MOVING_UP)

    assert cover._attr_is_opening is True
    assert cover._attr_is_closing is False
    assert cover._move_start_position == 0
    _magic_mock(mock_api.record_position_update).assert_called_once_with(
        "ABC123",
        source="primary status ABC123/01 command 01",
        direction="opening",
        previous_position=0,
        new_position=0,
        status="confirmed",
    )


@pytest.mark.asyncio
async def test_cover_handle_started_moving_down(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test handling started moving down event."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 100

    with patch.object(cover, "_start_position_tracking"):
        with patch.object(cover, "async_write_ha_state"):
            cover._handle_event(EVENT_STARTED_MOVING_DOWN)

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is True
    assert cover._move_start_position == 100


@pytest.mark.asyncio
async def test_cover_handle_stopped(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test handling stopped event."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 50
    cover._target_position = 50

    with patch.object(cover, "_stop_position_tracking"):
        with patch.object(cover, "async_write_ha_state"):
            cover._handle_event(EVENT_STOPPED)

    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._attr_current_cover_position == 50


@pytest.mark.asyncio
async def test_cover_update_position_opening(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test position update while opening."""
    import time

    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
        device_data={CONF_OPEN_TIME: 20.0},  # 20 seconds to fully open
    )
    cover.hass = hass
    cover._attr_is_opening = True
    cover._attr_current_cover_position = 0
    cover._move_start_position = 0
    cover._move_start_time = time.monotonic() - 10.0  # Simulating 10 seconds elapsed
    cover._position_update_source = "primary status ABC123/01 command 01"

    cover._update_position()

    # After 10 seconds of 20 second travel time, should be at 50%
    assert 45 <= cover._attr_current_cover_position <= 55  # Allow some tolerance
    _, kwargs = _magic_mock(mock_api.record_position_update).call_args
    assert kwargs["source"] == "primary status ABC123/01 command 01"
    assert kwargs["direction"] == "opening"
    assert kwargs["previous_position"] == 0
    assert 45 <= kwargs["new_position"] <= 55
    assert kwargs["status"] == "estimated"


@pytest.mark.asyncio
async def test_cover_update_position_closing(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test position update while closing."""
    import time

    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
        device_data={CONF_CLOSE_TIME: 20.0},  # 20 seconds to fully close
    )
    cover.hass = hass
    cover._attr_is_closing = True
    cover._attr_current_cover_position = 100
    cover._move_start_position = 100
    cover._move_start_time = time.monotonic() - 10.0  # Simulating 10 seconds elapsed

    cover._update_position()

    # After 10 seconds of 20 second travel time, should be at 50%
    assert 45 <= cover._attr_current_cover_position <= 55  # Allow some tolerance


@pytest.mark.asyncio
async def test_cover_calibration_completed(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test handling calibration completed event."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 50

    with patch.object(cover, "async_write_ha_state"):
        cover._handle_calibration_completed("ABC123", 25.0, 23.0)

    assert cover._travel_time_open == 25.0
    assert cover._travel_time_close == 23.0
    assert cover._attr_current_cover_position == 0
    assert cover._attr_is_closed is True


@pytest.mark.asyncio
async def test_cover_calibration_different_device(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test calibration event for different device doesn't affect this cover."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass
    cover._travel_time_open = 30.0
    cover._travel_time_close = 30.0
    cover._attr_current_cover_position = 50

    cover._handle_calibration_completed("XYZ789", 25.0, 23.0)

    # Should not change
    assert cover._travel_time_open == 30.0
    assert cover._travel_time_close == 30.0
    assert cover._attr_current_cover_position == 50


@pytest.mark.parametrize("position", [0, 42, 100])
def test_manual_position_sync_updates_cover_and_stops_estimator(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
    position: int,
) -> None:
    """Test a manual sync immediately becomes the cover's confirmed state."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="stable-id",
        device_enum="10",
        device_name="Test Cover",
        command_device_id="F2B8D5",
        status_device_id="3720B8",
        status_enum="08",
    )
    cover.hass = hass
    cover._attr_current_cover_position = 50
    cover._attr_is_opening = True
    cover._attr_is_closing = False
    cover._move_start_time = 123.0
    cover._move_start_position = 50
    cover._target_position = 75
    _magic_mock(mock_api.record_position_update).reset_mock()

    with (
        patch.object(cover, "_stop_position_tracking") as stop_tracking,
        patch.object(cover, "async_write_ha_state") as write_state,
    ):
        cover._handle_manual_position_sync(position)

    stop_tracking.assert_called_once_with()
    write_state.assert_called_once_with()
    assert cover._attr_current_cover_position == position
    assert cover._attr_is_closed is (position == 0)
    assert cover._attr_is_opening is False
    assert cover._attr_is_closing is False
    assert cover._move_start_time is None
    assert cover._move_start_position is None
    assert cover._target_position is None
    _magic_mock(mock_api.record_position_update).assert_called_once_with(
        "F2B8D5",
        source="Developer Tools manual position sync",
        direction="manual",
        previous_position=50,
        new_position=position,
        status="confirmed/manual",
    )


@pytest.mark.asyncio
async def test_unknown_status_does_not_alias_command_identity(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test a controllable cover loads without registering transmit ID as status."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="06C5C0",
        device_enum="11",
        device_name="Garden",
        command_device_id="06C5C0",
        status_identity_source=STATUS_IDENTITY_SOURCE_UNKNOWN,
    )
    cover.hass = hass

    with (
        patch.object(cover, "async_get_last_state", return_value=None),
        patch(
            "custom_components.schellenberg_usb.cover.async_dispatcher_connect"
        ) as dispatcher_connect,
        patch.object(cover, "async_write_ha_state"),
    ):
        await cover.async_added_to_hass()

    assert cover._status_device_id is None
    assert cover._status_enum is None
    _magic_mock(mock_api.register_entity).assert_called_once_with(
        None,
        None,
        "Garden",
        command_device_id="06C5C0",
        command_enum="11",
        secondary_status_identities=(),
    )
    assert not any(
        str(call.args[1]).startswith("schellenberg_usb_device_event_")
        for call in dispatcher_connect.call_args_list
    )


@pytest.mark.asyncio
async def test_cover_registers_with_api(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover registers itself with API."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass

    with (
        patch.object(cover, "async_get_last_state", return_value=None),
        patch(
            "custom_components.schellenberg_usb.cover.async_dispatcher_connect"
        ) as dispatcher_connect,
        patch.object(cover, "async_write_ha_state"),
    ):
        await cover.async_added_to_hass()

    assert any(
        call.args[1] == "schellenberg_usb_manual_position_sync_ABC123"
        and call.args[2] == cover._handle_manual_position_sync
        for call in dispatcher_connect.call_args_list
    )
    _magic_mock(mock_api.register_entity).assert_called_once_with(
        "ABC123",
        "01",
        "Test Cover",
        command_device_id="ABC123",
        command_enum="01",
        secondary_status_identities=(),
    )


@pytest.mark.asyncio
async def test_cover_will_remove_from_hass(
    hass: HomeAssistant,
    mock_api: SchellenbergUsbApi,
) -> None:
    """Test cover cleanup on removal."""
    cover = SchellenbergCover(
        api=mock_api,
        device_id="ABC123",
        device_enum="01",
        device_name="Test Cover",
    )
    cover.hass = hass

    with patch.object(cover, "_stop_position_tracking") as mock_stop:
        await cover.async_will_remove_from_hass()
        mock_stop.assert_called_once()
