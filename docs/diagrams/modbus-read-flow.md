# Modbus read flow

How one `dmon read` / `dmon json` / `dmon watch` cycle moves data from the inverter into a
decoded snapshot. Read-only by construction: only function code `0x03` is ever sent.

```mermaid
sequenceDiagram
    autonumber
    participant CLI as dmon CLI
    participant Client as ModbusClient
    participant Adapt as Waveshare adapter
    participant Inv as Deye inverter

    CLI->>Client: connect /dev/modbus 9600 8N1
    loop four blocks of 125 registers
        Client->>Adapt: FC3 frame slave 1 start count CRC
        Adapt->>Inv: differential RS485 A/B
        Inv-->>Adapt: reply function 0x03 + words + CRC
        Adapt-->>Client: bytes over USB serial
        Client->>Client: verify slave, length, CRC
        alt CRC mismatch or timeout
            Client->>Client: retry up to 2 times
        else valid reply
            Client->>Client: append words to address map
        end
    end
    Client-->>CLI: dict address to raw word
    CLI->>CLI: decode scale offset shift mask
    CLI-->>CLI: drop zero values and print table or JSON
```

Decision path inside the client, per register block:

```mermaid
flowchart TD
    A[read_holding_registers start count] --> B[write FC3 frame with CRC]
    B --> C{reply within timeout}
    C -- no --> D{retries left}
    D -- yes --> B
    D -- no --> E[raise ModbusTimeout]
    C -- yes --> F{function code}
    F -- 0x83 --> G[raise ModbusExceptionResponse]
    F -- 0x03 --> H{payload length matches}
    H -- no --> E
    H -- yes --> I{CRC valid}
    I -- no --> D
    I -- yes --> J[return list of words]
```

Snapshot decoding:

```mermaid
flowchart LR
    W[raw words] --> S{value_type}
    S -- U_WORD --> U[16 bit unsigned]
    S -- S_WORD --> SG[16 bit signed]
    S -- U_DWORD_R --> DW[low word first then high word]
    U --> B{shift or mask set}
    SG --> B
    DW --> B
    B -- yes --> BF[extract bit field]
    B -- no --> N[apply offset then scale]
    BF --> N
    N --> R[Reading name value unit group]
    R --> Z{value equals zero}
    Z -- yes --> X[dropped from output]
    Z -- no --> O[table or JSON]
```
