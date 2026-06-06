from __future__ import annotations

from typing import Protocol

from config import Settings
from db import Database


class BalanceUnavailableError(RuntimeError):
    pass


class BalanceProvider(Protocol):
    def available_balance_usd(self) -> float:
        ...


class DryRunBalanceProvider:
    def __init__(self, db: Database, starting_balance_usd: float):
        self.db = db
        self.starting_balance_usd = starting_balance_usd

    def available_balance_usd(self) -> float:
        return self.db.simulated_cash_balance(self.starting_balance_usd)


class LiveCollateralBalanceProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None

    def available_balance_usd(self) -> float:
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        except ImportError as exc:
            raise BalanceUnavailableError("live balance check requires the live dependencies") from exc

        try:
            response = self._get_client().get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw_balance = response.get("balance") if isinstance(response, dict) else None
            if raw_balance is None:
                raise ValueError("balance response did not include balance")
            return max(0.0, float(raw_balance) / 1_000_000)
        except BalanceUnavailableError:
            raise
        except Exception as exc:
            raise BalanceUnavailableError(f"live collateral balance unavailable: {exc}") from exc

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise BalanceUnavailableError("live balance check requires the live dependencies") from exc

        client = ClobClient(
            self.settings.clob_base_url,
            key=self.settings.polymarket_private_key,
            chain_id=self.settings.chain_id,
            signature_type=self.settings.polymarket_signature_type,
            funder=self.settings.polymarket_funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        self._client = client
        return client
