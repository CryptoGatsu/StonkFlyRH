# Third-party components

## Upstream project

StonkFlyRH is a fork of [nftechie/stonkfly](https://github.com/nftechie/stonkfly),
MIT licensed. The neural stack — connectome import, circuit construction, the spiking
kernel, the visual front end, the plasticity rule and the fixed decoder — is upstream's
work, carried over unchanged apart from the package rename. What this fork replaces is
everything below the decoder: market observation, execution, accounting and the site.

Upstream traded Coinbase Advanced spot pairs through a Coinbase AgentKit
`ActionProvider`. This fork trades Robinhood Chain memecoins and has no Coinbase
dependency, so the guarded action boundary is reimplemented natively in
`stonkflyrh/actions.py` with the same shape: one declared action, a schema-validated
proposal, no LLM, no general wallet tool and no transfer action.

## Dataset

MaleCNS v1.0 connectome, retained graph of 166,700 neurons and 25,582,938 directed
edges. Downloaded and checksum-verified by `python -m stonkflyrh prepare`; see
`stonkflyrh/neural/sources.lock.json` for the exact files and hashes, and
[docs/model.md](docs/model.md) for what the simulation does and does not establish.

## Python dependencies

| Package | Used for |
| --- | --- |
| `web3`, `eth-account`, `eth-utils` | JSON-RPC, ABI encoding, local keystores and transaction signing |
| `numpy` | the spiking kernel's arrays and the sensory frame |
| `pandas`, `pyarrow` | neuron annotations and the released Feather inputs |
| `Pillow` | rendering the RGB chart the connectome observes |
| `pydantic` | validating the neural proposal at the action boundary |
| `python-dotenv` | reading `.env` from the working directory only |

The website is served from the standard library and pulls in no framework. It links
JetBrains Mono and Archivo from Google Fonts with a system fallback stack; a viewer
without that host sees the system faces. The FLY.EXE scene loads three.js r128 from
cdnjs; without it the page renders everything but the scene.

## Contracts

None are vendored, and none are hardcoded. The Uniswap v3 router, quoter, factory and
pool interfaces are declared as minimal ABI fragments in `stonkflyrh/chain.py` and
`stonkflyrh/market.py` so the surface this process can reach is auditable in one place.
The addresses behind them are operator configuration, verified against the chain before
any run.
