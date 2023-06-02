import logging

from hexbytes import HexBytes
from web3 import Web3

from abi import STARGATE_ROUTER_ABI
from network.network import EVMNetwork, TransactionStatus
from utility import Stablecoin

from stargate.constants import StargateConstants
from eth_account.signers.local import LocalAccount
from base.errors import TransactionFailed, TransactionNotFound

logger = logging.getLogger(__name__)


class StargateUtils:
    @staticmethod
    def estimate_layerzero_swap_fee(src_network: EVMNetwork, dst_network: EVMNetwork, dst_address: str) -> int:
        """ Method that estimates LayerZero fee to make the swap in native token """

        contract = src_network.w3.eth.contract(
            address=Web3.to_checksum_address(src_network.stargate_router_address),
            abi=STARGATE_ROUTER_ABI)

        quote_data = contract.functions.quoteLayerZeroFee(
            dst_network.stargate_chain_id,  # destination chainId
            1,  # function type (1 - swap): see Bridge.sol for all types
            dst_address,  # destination of tokens
            "0x",  # payload, using abi.encode()
            [0,  # extra gas, if calling smart contract
             0,  # amount of dust dropped in destination wallet
             "0x"  # destination wallet for dust
             ]
        ).call()

        return quote_data[0]

    @staticmethod
    def estimate_swap_gas_price(network: EVMNetwork) -> int:
        approve_gas_limit = network.get_approve_gas_limit()
        max_overall_gas_limit = StargateConstants.get_max_randomized_swap_gas_limit(network.name) + approve_gas_limit

        gas_price = max_overall_gas_limit * network.get_current_gas()

        return gas_price

    @staticmethod
    def is_enough_native_token_balance_for_stargate_swap_fee(src_network: EVMNetwork,
                                                             dst_network: EVMNetwork, address: str):
        account_balance = src_network.get_balance(address)
        gas_price = StargateUtils.estimate_swap_gas_price(src_network)
        layerzero_fee = StargateUtils.estimate_layerzero_swap_fee(src_network, dst_network, address)

        enough_native_token_balance = account_balance > (gas_price + layerzero_fee)

        return enough_native_token_balance


class StargateBridgeHelper:

    def __init__(self, account: LocalAccount, src_network: EVMNetwork, dst_network: EVMNetwork,
                 src_stablecoin: Stablecoin, dst_stablecoin: Stablecoin, amount: int, slippage: float):
        self.account = account
        self.src_network = src_network
        self.dst_network = dst_network
        self.src_stablecoin = src_stablecoin
        self.dst_stablecoin = dst_stablecoin
        self.amount = amount
        self.slippage = slippage

    def make_bridge(self) -> bool:
        """ Method that performs bridge from src_network to dst_network """

        if not self._is_bridge_possible():
            return False

        self._approve_stablecoin_usage(self.amount)

        tx_hash = self._send_swap_transaction()
        result = self.src_network.wait_for_transaction(tx_hash)

        self._check_tx_result(result, "Stargate swap")

    @staticmethod
    def _check_tx_result(result: TransactionStatus, name: str) -> None:
        """ Utility method that checks transaction result and raises exceptions if it's not mined or failed.
         Probably should be moved to the EVMNetwork class """

        if result == TransactionStatus.NOT_FOUND:
            raise TransactionNotFound(f"{name} transaction can't be found in the blockchain"
                                      "for a log time. Consider changing fee settings")
        if result == TransactionStatus.FAILED:
            raise TransactionFailed(f"{name} transaction failed")

        if result == TransactionStatus.SUCCESS:
            logger.info(f"{name} transaction succeed")

    def _send_swap_transaction(self) -> HexBytes:
        """ Utility method that signs and sends tx - Swap src_pool_id token from src_network chain to dst_chain_id """

        contract = self.src_network.w3.eth.contract(
            address=Web3.to_checksum_address(self.src_network.stargate_router_address),
            abi=STARGATE_ROUTER_ABI)

        layerzero_fee = StargateUtils.estimate_layerzero_swap_fee(self.src_network, self.dst_network,
                                                                  self.account.address)
        nonce = self.src_network.get_nonce(self.account.address)
        gas_params = self.src_network.get_transaction_gas_params()
        amount_with_slippage = self.amount - int(self.amount * self.slippage)

        logger.info(f'Estimated fees. LayerZero fee: {layerzero_fee}. Gas price: {gas_params}')

        tx = contract.functions.swap(
            self.dst_network.stargate_chain_id,  # destination chainId
            self.src_stablecoin.stargate_pool_id,  # source poolId
            self.dst_stablecoin.stargate_pool_id,  # destination poolId
            self.account.address,  # refund address. extra gas (if any) is returned to this address
            self.amount,  # quantity to swap
            amount_with_slippage,  # the min qty you would accept on the destination
            [0,  # extra gas, if calling smart contract
             0,  # amount of dust dropped in destination wallet
             "0x"  # destination wallet for dust
             ],
            self.account.address,  # the address to send the tokens to on the destination
            "0x",  # "fee" is the native gas to pay for the cross chain message fee
        ).build_transaction(
            {
                'from': self.account.address,
                'value': layerzero_fee,
                'gas': StargateConstants.get_randomized_swap_gas_limit(self.src_network.name),
                **gas_params,
                'nonce': nonce
            }
        )

        signed_tx = self.src_network.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self.src_network.w3.eth.send_raw_transaction(signed_tx.rawTransaction)

        logger.info(f'Stargate swap transaction signed and sent. Hash: {tx_hash.hex()}')

        return tx_hash

    def _is_bridge_possible(self) -> bool:
        """ Method that checks account balance on the source chain and decides if it is possible to make bridge """

        if not StargateUtils.is_enough_native_token_balance_for_stargate_swap_fee(self.src_network, self.dst_network,
                                                                                  self.account.address):
            return False

        stablecoin_balance = self.src_network.get_token_balance(self.src_stablecoin.contract_address,
                                                                self.account.address)
        if stablecoin_balance < self.amount:
            return False

        return True

    def _approve_stablecoin_usage(self, amount: int) -> None:
        allowance = self.src_network.get_token_allowance(self.src_stablecoin.contract_address, self.account.address,
                                                         self.src_network.stargate_router_address)
        if allowance >= amount:
            return

        tx_hash = self.src_network.approve_token_usage(self.account.key, self.src_stablecoin.contract_address,
                                                       self.src_network.stargate_router_address, amount)
        result = self.src_network.wait_for_transaction(tx_hash)

        self._check_tx_result(result, f"Approve {self.src_stablecoin.symbol} usage")