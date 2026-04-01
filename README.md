# DongLoRa Mux (Python)

USB multiplexer daemon — lets multiple applications share one DongLoRa
dongle simultaneously.

## What It Does

- Owns the USB serial connection exclusively
- Exposes a Unix domain socket (and optional TCP) speaking the same
  COBS-framed protocol
- RxPacket frames broadcast to all connected clients
- StartRx/StopRx reference-counted across clients
- SetConfig locked once set (single client can change freely)

## Running

```sh
just run                            # start the mux daemon
just verbose                        # start with verbose logging
just run --tcp 5741 --port /dev/ttyACM0   # with options
```

## Depends On

- [clients/python](../../clients/python/) — the `donglora` client library
  (protocol encoding, device discovery)
