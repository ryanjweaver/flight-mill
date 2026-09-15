"""Software-only startup queue model and real host framing regression.

The finite FIFO models the reviewed HWCDC disconnected write policy. It is
not an execution of the ESP32 driver or evidence of a physical connection.
"""
from collections import deque

import pytest

from flightmill.protocol.models import parse_line, serialize_line
from test_firmware_contract import MAIN, cpp_integer
from test_serial_acquisition import Clock, Endpoint, make_service, pump


def startup_frames():
    prefix = (b'{"protocol":"flightmill","protocol_version":1,'
              b'"type":"%s","device_id":"FM-S3-020000000001",'
              b'"firmware_version":"0.1.3-dev","message_seq":%d,'
              b'"boot_id":"00000000-00000000-00000001","state":"IDLE",')
    return (prefix % (b'hello', 0) + b'"reset_reason":"power_on"}\n'
            + prefix % (b'sensor_state', 1) + b'"sensor_state":0}\n')


def test_256_byte_transmit_fifo_reproduces_the_observed_startup_tail():
    frames = startup_frames()
    queued = bytes(deque(frames, maxlen=256))
    assert len(frames) == 442 and len(frames) - len(queued) == 186
    assert queued.startswith(b'":"IDLE","reset_reason":"power_on"}\n')
    assert parse_line(queued.splitlines()[1]).type == 'sensor_state'
    with pytest.raises(ValueError):
        parse_line(queued.splitlines()[0])


def test_firmware_queue_retains_startup_and_eight_idle_heartbeats():
    clock = Clock()
    endpoint = Endpoint(clock)
    endpoint.device.boot()
    frames = startup_frames()
    for _ in range(8):
        clock.advance(1)
        frames += serialize_line(endpoint.device.heartbeat())
    capacity = cpp_integer('SERIAL_TX_CAPACITY')
    assert capacity >= cpp_integer('SERIAL_LINE_CAPACITY') * 10
    assert bytes(deque(frames, maxlen=capacity)) == frames


def test_tx_queue_is_allocated_before_begin_and_allocation_failure_blocks_idle():
    source = MAIN.read_text(encoding='utf-8')
    assert source.index('Serial.setTxBufferSize(fm::SERIAL_TX_CAPACITY)') < source.index('Serial.begin(')
    assert 'memoryValid && serialRxBufferValid && serialTxBufferValid' in source
    assert '"serial_tx_buffer_allocation_failed"' in source


@pytest.mark.parametrize('chunk_size', [1, 7, 64, 256])
def test_host_accepts_complete_hello_across_serial_read_boundaries(tmp_path, chunk_size):
    clock = Clock()

    class FragmentedEndpoint(Endpoint):
        def read(self, size):
            return super().read(min(size, chunk_size))

    endpoint = FragmentedEndpoint(clock)
    service = make_service(tmp_path, endpoint, clock)
    try:
        service.action('select_source', {'source':'serial'})
        service.action('connect')
        result = pump(service, lambda s:s['ready'])
        assert result['state'] == 'IDLE' and not result['warnings']
        assert [command.type for command in endpoint.writes] == ['get_status']
        assert not list(tmp_path.glob('*_journal.json'))
    finally:
        service.close()
