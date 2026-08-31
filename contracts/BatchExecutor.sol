// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title BatchExecutor — EIP-7702 delegate for batching mints across collections
/// @notice NOT REQUIRED for the current live deployment, and NOT DEPLOYED.
///
/// Single-collection minting uses a plain type-2 transaction calling
/// `mintPublic` directly; that is what live minters on Robinhood actually do and
/// it needs no contract. This delegate exists for the case that genuinely needs
/// EIP-7702: batching several *different* collections into one transaction.
///
/// Why that is the only batching worth doing here — measured, not assumed:
///   * `payer == minter` in 94,872 of 96,278 live SeaDrop mints (98.5%), so a
///     batch can never mint on behalf of other wallets. Batching is strictly
///     within one wallet.
///   * ~39% of free Robinhood drops have `maxTotalMintableByWallet == 1`, so
///     batching quantity of a single collection is usually impossible.
///   * Gas is NOT the reason. A mint costs ~0.0000175 ETH; batching five saves
///     ~0.0000143 ETH, about four cents. The real benefits are: one nonce and
///     one inclusion slot (a revert on mint #2 cannot stall #3-#5 behind a nonce
///     gap), and atomic mint-and-sweep so a token is never idle in a hot wallet
///     between two separate inclusions.
///
/// @dev Under EIP-7702 an EOA delegates its code to this contract, so `msg.sender`
/// and `address(this)` are BOTH the EOA itself during execution. The auth check
/// is therefore `msg.sender == address(this)`: only the EOA that delegated here
/// may drive it. Nothing else can call `execute`.
///
/// UNAUDITED. Deploy only after review and testing on a throwaway wallet.
contract BatchExecutor {
    struct Call {
        address target;
        uint256 value;
        bytes data;
    }

    error Unauthorized();
    error CallFailed(uint256 index, bytes reason);

    event BatchExecuted(uint256 count, uint256 succeeded);

    /// @notice Execute calls atomically. Any failure reverts the whole batch.
    /// @dev All-or-nothing is the point: if one mint reverts, none execute and
    /// there is no partial state to reconcile.
    function execute(Call[] calldata calls) external payable {
        if (msg.sender != address(this)) revert Unauthorized();
        uint256 n = calls.length;
        for (uint256 i; i < n; ++i) {
            (bool ok, bytes memory ret) =
                calls[i].target.call{value: calls[i].value}(calls[i].data);
            if (!ok) revert CallFailed(i, ret);
        }
        emit BatchExecuted(n, n);
    }

    /// @notice Execute calls, skipping any that fail.
    /// @dev Use when partial success is preferable — e.g. five concurrent drops
    /// where one selling out mid-block should not cost you the other four.
    /// Pre-flight simulation should already have removed doomed calls; this is
    /// the backstop for a drop that sells out between simulation and inclusion.
    function executeAllowingFailures(Call[] calldata calls)
        external
        payable
        returns (bool[] memory results)
    {
        if (msg.sender != address(this)) revert Unauthorized();
        uint256 n = calls.length;
        results = new bool[](n);
        uint256 succeeded;
        for (uint256 i; i < n; ++i) {
            (bool ok,) =
                calls[i].target.call{value: calls[i].value}(calls[i].data);
            results[i] = ok;
            if (ok) ++succeeded;
        }
        emit BatchExecuted(n, succeeded);
    }

    /// @notice Recover ETH left in the account after a batch.
    function sweepETH(address payable to) external {
        if (msg.sender != address(this)) revert Unauthorized();
        (bool ok,) = to.call{value: address(this).balance}("");
        if (!ok) revert CallFailed(0, "");
    }

    receive() external payable {}
}
