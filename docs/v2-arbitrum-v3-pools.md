# task v2 — arbitrum uniswap v3 pools: price + liquidity

read `CLAUDE.md` first. follow its safety rules and conventions.

## goal

`python scanner026.py pools --chain arbitrum` shows **uniswap v3** pools next to the existing v2 pools:

```
pool         dex    fee    pair_address  block      tick     reserve_base   reserve_quote     price                tvl_usd     warn
WETH/USDC    sushi  -      0x57b...3378  526376217  -        2.9233 WETH    4913.2815 USDC    1680.75 USDC/WETH    9826.56
WETH/USDC.e  sushi  -      0x905...1Aa3  526376217  -        41.6755 WETH   69948.8948 USDC.e 1678.42 USDC.e/WETH  139897.79   ...
WETH/USDC    univ3  0.05%  0xc6...       526376217  -201234  1234.5 WETH    2.1M USDC         1680.12 USDC/WETH    ...
WETH/USDC    univ3  0.30%  0x...         ...
WETH/USDC.e  univ3  0.05%  0x...         ...
```

(numbers are examples.) still read-only: only `eth_call` and block reads.

## background (v3 vs v2)

- v3 has no `getReserves`. price comes from `slot0().sqrtPriceX96`:
  raw price (token1 per token0, raw units) = `sqrtPriceX96² / 2¹⁹²`.
  human price = raw × `10^(decimals0 − decimals1)`. invert if base is token1.
- one pair can have several pools, one per fee tier (`100`, `500`, `3000`, `10000` = 0.01 / 0.05 / 0.3 / 1 %).
- liquidity is concentrated: `liquidity()` is only the **active** liquidity at the current tick.
  real token amounts in the pool = `balanceOf(pool)` of each token.
- because v3 is not 50/50, tvl is **stable side + other side × price**, not 2 × stable.

## files allowed to create / change

- `config/pools.yaml`, `config/loader.py`
- `sources/erc20.py` (new: shared erc20 + call helpers, see refactor)
- `sources/v2.py`, `sources/v3.py` (new), `sources/pools.py` (new)
- `metrics/price.py`, `metrics/liquidity.py`
- `scanner026.py`
- tests: `tests/test_pools_config.py`, `tests/test_v2.py`, `tests/test_v3.py` (new), `tests/test_pools.py` (new),
  `tests/test_price.py`, `tests/test_liquidity.py`, `tests/test_cli.py`
- `CLAUDE.md`, `README.md` (as always)

do not change `sources/rpc.py` or `config/chains.yaml`. do not touch blockchain-indexer.

## refactor first (no behaviour change)

1. **`sources/erc20.py`**: move `ERC20_ABI`, `PoolError`, `same_address`, `call_at`, `read_symbol`, `check_token`
   out of `v2.py`. `v2.py` imports them.
2. **`sources/pools.py`**: move `read_pools`, `read_head`, `error_states`, `read_pool_spec` out of `v2.py`.
   `read_pool_spec` dispatches on `dex.type`: `uniswap_v2` → `v2.read_pool`, `uniswap_v3` → `v3.read_pool`.
   `scanner026.py` imports `read_pools` from `sources.pools`.
3. move the `read_pools` tests from `test_v2.py` to `test_pools.py`.
4. run `pytest -q` → still 86 passed **before** adding v3. mention this in the changelog.

## details

### config/pools.yaml

add to `arbitrum`:

```yaml
  dexes:
    sushi:
      type: uniswap_v2
      factory: "0xc35DADB65012eC5796536bD9864eD8773aBc74C4"
    univ3:
      type: uniswap_v3
      factory: "0x1F98431c8aD98523631AE4a59f267346ea31F984"
  pools:
    - { dex: sushi, base: WETH, quote: USDC }
    - { dex: sushi, base: WETH, quote: USDC.e }
    - { dex: univ3, base: WETH, quote: USDC,   fee: 500 }
    - { dex: univ3, base: WETH, quote: USDC,   fee: 3000 }
    - { dex: univ3, base: WETH, quote: USDC.e, fee: 500 }
```

loader:
- `SUPPORTED_DEX_TYPES` gets `uniswap_v3`.
- `PoolSpec` gets `fee: int | None = None`.
- `uniswap_v3` pool **must** have `fee` in `{100, 500, 3000, 10000}`, else `ValueError`.
- `uniswap_v2` pool **must not** have `fee`, else `ValueError` (v2 fee is fixed by the dex).

### PoolState

add optional fields (all `None` for v2):
`fee: int | None`, `tick: int | None`, `sqrt_price_x96: int | None`, `liquidity: int | None`.
for v3, `reserve_base_raw` / `reserve_quote_raw` = `balanceOf(pool)` of base / quote.
move `PoolState` to `sources/pools.py` if that keeps imports clean (then `metrics` imports it from there).

### sources/v3.py

minimal abis inline:
- factory: `getPool(address,address,uint24) -> address`
- pool: `token0()`, `token1()`, `factory()`, `fee() -> uint24`, `liquidity() -> uint128`,
  `slot0() -> (uint160 sqrtPriceX96, int24 tick, uint16, uint16, uint16, uint8, bool)`
- erc20: `balanceOf(address) -> uint256` (add to `ERC20_ABI` in `erc20.py`)

`async def read_pool(w3, chain_cfg, dex, base, quote, fee, block, timeout) -> PoolState`, same rules as v2:
1. every call at the given `block`.
2. `getPool(base, quote, fee)`. zero address → `error="pool not found"`.
3. verify on-chain: `factory() == dex.factory`, `{token0, token1} == {base, quote}`, `fee() == fee`. mismatch → error.
4. `check_token` for both tokens (decimals hard error, symbol warn), shared with v2.
5. `sqrtPriceX96 == 0` → `error="pool not initialized"`.
6. read `slot0`, `liquidity`, and both `balanceOf(pool)`.
7. never raises.

### metrics/price.py

- `v3_spot_price(sqrt_price_x96, base_decimals, quote_decimals, base_is_token0) -> Fraction | None`
  exact with `Fraction`: token1-per-token0 = `Fraction(sqrt_price_x96 ** 2 * 10**decimals0, 2**192 * 10**decimals1)`,
  then invert if base is token1. `0` → `None`. **no float, no `math.sqrt`, no `1.0001 ** tick`.**
- `PoolState` needs to know whether base is token0: add `base_is_token0: bool | None` (set for v2 and v3).

### metrics/liquidity.py

- change `tvl_usd` to **stable units + other units × price** (needs the price, pass it in).
  for v2 this gives exactly the same result as 2 × stable (because v2 price = reserve ratio),
  so existing tvl tests must keep passing with the same expected values. add a test that proves this.
- neither side stable → `None`, as before.

### scanner026.py

- table gets `fee` (`0.05%`, `-` for v2) and `tick` (`-` for v2) columns.
- price per row: v2 → `spot_price` from reserves, v3 → `v3_spot_price`.
- big reserves: keep 4 decimals, no abbreviations like `2.1M` (the example above is only illustrative).

## tests (no network)

- `test_pools_config.py`: v3 pool needs a valid fee, v2 pool must not have one, `uniswap_v3` type accepted.
- `test_v3.py` with fake contracts:
  ok case, reversed token order, pool not found, wrong factory, wrong fee, uninitialized pool,
  decimals mismatch → error, symbol mismatch → warn, every call at the given block.
- `test_price.py`:
  - `sqrt_price_x96 = 2**96` with decimals 18 / 6 → exactly `10**12` (token1 per token0).
  - inverted (base is token1) → exactly `Fraction(1, 10**12)`.
  - a realistic case: build `sqrt_price_x96 = math.isqrt(target_raw * 2**192)` for ~1680 usdc/weth
    (`isqrt` is exact int math, allowed in tests) and check the result is within 1e-9 relative of the target.
  - `0` → `None`.
- `test_liquidity.py`: v2 tvl unchanged with the new formula; v3 tvl with unbalanced sides.
- `test_pools.py`: dispatch v2 / v3 by dex type, wrong chain id still blocks all pools (moved tests).
- `test_cli.py`: v3 row formatting (`fee` 0.05%, tick), v2 row shows `-` for both.

## live run

after the tests pass, Claude CLI **may** run once (read-only calls only):

```bash
python scanner026.py pools --chain arbitrum; echo "exit: $?"
```

put the output in the changelog. sanity check to write down (do not fail the code on it):
the v3 0.05% WETH/USDC price should be within ~1 % of the sushi WETH/USDC price.
if an address is wrong (pool not found, factory / fee / decimals mismatch), **do not guess a new address**:
report it under open issues and stop.

## definition of done

- [ ] refactor step done first, 86 tests still passing before v3 code
- [ ] `.venv/bin/pytest -q` passes, no network
- [ ] `pools --chain arbitrum` prints 5 rows (or a clear error per row), v2 rows unchanged in value
- [ ] no float in price / tvl math (`grep -n "float\|math.sqrt\|\*\* *tick" metrics/ sources/` only shows `timeout: float`)
- [ ] `CLAUDE.md`: changelog entry (with live output + v2 vs v3 price check), structure section updated
- [ ] `README.md`: v3 support, fee + tick columns, tvl formula
- [ ] full diff shown at the end, nothing committed
