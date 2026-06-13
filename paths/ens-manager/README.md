# ENS Manager

Manage ENS names from your AI agent. Supports the full lifecycle: check availability, register, renew, transfer, set text/address records, create subnames, and set your primary name from L2.

## Actions

| Action | Chain | Description |
|---|---|---|
| `lookup` | Mainnet (read) | Resolve name → address or address → primary name |
| `check` | Mainnet (read) | Check availability and registration price |
| `register` | Mainnet | Register a `.eth` name (2-tx commit/register flow) |
| `renew` | Mainnet | Extend expiry of an existing name |
| `transfer` | Mainnet | Transfer ownership to another address |
| `set-records` | Mainnet | Set text records (avatar, url, email, twitter, …) |
| `create-subname` | Mainnet | Create a subname under a name you own |
| `set-primary` | Base / L2 | Set your primary ENS name from Base, OP, Arbitrum, Linea, or Scroll |

## Usage

```bash
# Check availability
poetry run python scripts/main.py --action check --name myname

# Register for 2 years
poetry run python scripts/main.py --action register --name myname --wallet main --duration 2

# Renew for 1 year
poetry run python scripts/main.py --action renew --name myname --wallet main

# Transfer to another address
poetry run python scripts/main.py --action transfer --name myname --to 0xAbc... --wallet main

# Set avatar text record
poetry run python scripts/main.py --action set-records --name myname.eth --wallet main --key avatar --value ipfs://Qm...

# Create a subname
poetry run python scripts/main.py --action create-subname --name myname.eth --sublabel alice --to 0xAbc... --wallet main

# Set primary name from Base
poetry run python scripts/main.py --action set-primary --name myname.eth --wallet main --chain 8453
```

## Configuration

Edit `inputs/config.yaml` to set your wallet label and override default registration duration. Contract addresses are pre-configured for Ethereum mainnet and all supported L2s.

## Notes

- **Register** is a 2-transaction flow (commit → 60s wait → register). Plan for ~2 minutes end-to-end.
- **Transfer** automatically detects wrapped (NameWrapper ERC-1155) vs unwrapped (Base Registrar ERC-721) names.
- **set-primary** uses the ENS L2 Reverse Registrar deployed at the same address on Base, Optimism, Arbitrum, Linea, and Scroll.
- Mainnet actions require ETH on Ethereum. `set-primary` requires ETH on the target L2 chain.

## Build & Publish

```bash
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path publish --path .
```
