"""Tests for cover platform."""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.cover import ATTR_POSITION
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr

from custom_components.schellenberg_usb.api import SchellenbergUsbApi
from custom_components.schellenberg_usb.const import (
    CONF_CLOSE_TIME,
    CONF_OPEN_TIME,
    CONF_SERIAL_PORT,
    DOMAIN,
    EVENT_STARTED_MOVING_DOWN,
    EVENT_STARTED_MOVING_UP,
    EVENT_STOPPED,
)
from custom_components.schellenberg_usb.cover import (
    DEFAULT_TRAVEL_TIME,
    SchellenbergCover,
    async_setup_entry,
)


@pytest.fixture
def mock_api(hass: HomeAssistant) -> SchellenbergUsbApi:
    """Create a mock API."""
    api = SchellenbergUsbApi(hass, "/dev/ttyUSB0")
    api._is_connected = True
    api.control_blind = AsyncMock()  # type: ignore[method-assign]
    api.register_entity = MagicMock()  # type: ignore[method-assign]
    return api


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> ConfigEntry:
    """Create a mock config entry with subentries."""
    # Create a real subentry dict instead of MagicMock to avoid serialization issues
    subentry = MagicMock()
    subentry.subentry_id = "sub1"
    subentry.data = {
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
    entry.subentries = {"sub1": subentry}  # type: ignore[misc]
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
    hub_device = dev_reg.async_get_or_create(
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
    )

    assert cover._device_id == "ABC123"
    assert cover._device_enum == "01"
    assert cover.unique_id == "schellenberg_ABC123"
    assert cover.name == "Test Cover"
    assert cover._attr_current_cover_position is None
    assert cover._travel_time_open == DEFAULT_TRAVEL_TIME
    assert cover._travel_time_close == DEFAULT_TRAVEL_TIME


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

    mock_api._is_connected = False
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
    mock_api.control_blind.assert_called_once_with("01", "01")


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
    mock_api.control_blind.assert_called_once_with("01", "02")


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
    mock_api.control_blind.assert_called_once_with("01", "00")


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

    cover._update_position()

    # After 10 seconds of 20 second travel time, should be at 50%
    assert 45 <= cover._attr_current_cover_position <= 55  # Allow some tolerance


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

    with patch.object(cover, "async_get_last_state", return_value=None):
        with patch("custom_components.schellenberg_usb.cover.async_dispatcher_connect"):
            with patch.object(cover, "async_write_ha_state"):
                await cover.async_added_to_hass()

    mock_api.register_entity.assert_called_once_with("ABC123", "01")


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
