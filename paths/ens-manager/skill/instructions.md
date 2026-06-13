# ENS Manager

Use this skill when the user wants to manage ENS names: check availability, register, renew, transfer, set records, create subnames, or set a primary name from L2.

## Trigger phrases
- "check if `<name>.eth` is available"
- "register `<name>.eth`"
- "renew my ENS name"
- "transfer `<name>.eth` to `<address>`"
- "set my ENS avatar / twitter / url"
- "create a subname under `<name>.eth`"
- "set my primary ENS name on Base"
- "look up `<name>.eth`" / "what address does `<name>.eth` resolve to"

## Chain routing

| Action | Chain | Gas token |
|---|---|---|
| lookup, check | Ethereum mainnet (read-only) | none |
| register, renew | Ethereum mainnet | ETH |
| transfer | Ethereum mainnet | ETH |
| set-records | Ethereum mainnet | ETH |
| create-subname | Ethereum mainnet | ETH |
| set-primary | Base (or OP/Arb/Linea/Scroll) | ETH on L2 |

Always verify the wallet has ETH on the relevant chain before executing fund-moving actions.

## Script invocation

Run from the project root via:
```
poetry run python paths/ens-manager/scripts/main.py --action <action> --name <name> [options]
```

### Action reference

| Action | Required args | Optional args |
|---|---|---|
| `lookup` | `--name` (name or 0x address) | |
| `check` | `--name` | |
| `register` | `--name`, `--wallet` | `--duration` (years, default 1) |
| `renew` | `--name`, `--wallet` | `--duration` |
| `transfer` | `--name`, `--wallet`, `--to` | |
| `set-records` | `--name`, `--wallet`, `--key`, `--value` | |
| `create-subname` | `--name` (parent), `--sublabel`, `--to` (owner), `--wallet` | |
| `set-primary` | `--name`, `--wallet` | `--chain` (default 8453 Base) |

### Examples
```bash
# Check availability
python paths/ens-manager/scripts/main.py --action check --name vitalik

# Register for 2 years
python paths/ens-manager/scripts/main.py --action register --name myname --wallet main --duration 2

# Set avatar text record
python paths/ens-manager/scripts/main.py --action set-records --name myname.eth --wallet main --key avatar --value ipfs://Qm...

# Create subname
python paths/ens-manager/scripts/main.py --action create-subname --name myname.eth --sublabel alice --to 0xAbc... --wallet main

# Set primary name from Base
python paths/ens-manager/scripts/main.py --action set-primary --name myname.eth --wallet main --chain 8453
```

## Safety rules
- **Always run `check` before `register`** — confirm the name is available and show the user the price before executing.
- **register is a 2-tx flow** — commit tx, then 70s wait, then register tx. The script handles this automatically but inform the user it takes ~2 minutes.
- **Transfer is irreversible** — confirm the recipient address with the user before executing.
- **set-primary on L2** — the wallet must have ETH on the target L2 chain for gas. Check `wayfinder://balances/<wallet>` first.

## Configuration
Path config lives at `paths/ens-manager/inputs/config.yaml`. Contract addresses are set there. Default wallet is `main`.
