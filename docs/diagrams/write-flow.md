# Guarded write flow

How `dmon-write` moves from a human intention to a verified register change. Read-only monitoring
is untouched by this path: the module is not imported by `dmon serve`.

```mermaid
flowchart TD
    A["dmon-write set tou.1.power=5500"] --> B{key on allow-list}
    B -- no --> C[exit 2, refused]
    B -- yes --> D{value in range}
    D -- no --> C
    D -- yes --> E[read current TOU block]
    E --> F[print planned change, dry run]
    F --> G{--confirm given}
    G -- no --> H[exit 0, nothing written]
    G -- yes --> I[build FC6 frame with CRC]
    I --> J[write register]
    J --> K{echo equals request}
    K -- no --> L[WriteVerificationError, exit 3]
    K -- yes --> M[read register back]
    M --> N{value matches}
    N -- no --> L
    N -- yes --> O[append JSON line to audit log]
    O --> P[exit 0]
```

Bit targets (grid/gen charge) add a read-modify-write step so the GM/BU/CH bits survive:

```mermaid
sequenceDiagram
    autonumber
    participant Op as Operator
    participant CLI as dmon-write
    participant Inv as Inverter

    Op->>CLI: set tou.3.gen_charge=1 --confirm
    CLI->>Inv: FC3 read 0x0114 (current 5 = grid+GM)
    CLI->>CLI: new = 5 OR bit1 = 7
    CLI->>Inv: FC6 write 0x0114 = 7
    Inv-->>CLI: echo 7
    CLI->>Inv: FC3 read 0x0114
    Inv-->>CLI: 7
    CLI->>CLI: verify bit1 == 1, then audit
    CLI-->>Op: ok, verified by readback
```

Verification without writing, used before trusting an automation or after a manual change on the
inverter screen:

```mermaid
flowchart LR
    A["dmon-write verify tou.1.power=5500"] --> B[FC3 read TOU block]
    B --> C{matches expected}
    C -- yes --> D[MATCH, exit 0]
    C -- no --> E[DRIFT with expected/actual, exit 1]
```
