---
name: web3-fullstack-packaged-build
description: Systematic full-stack Web3 build with mandatory packaging and delivery phase
---

# Web3 Full-Stack Packaged Build

This skill provides a structured, layer-by-layer approach to building complex full-stack Web3 applications **with integrated deliverable packaging**. Follow this sequence to maintain clarity, reduce coupling, enable incremental testing, and ensure a complete, shippable artifact is produced even when tool failures occur.

## Phase 1: Project Structure

First, establish the complete directory structure before writing any code:

```
project-name/
├── contracts/
│   ├── interfaces/
│   ├── core/
│   ├── libraries/
│   └── deployment/
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── hooks/
│   │   ├── config/
│   │   └── utils/
│   └── public/
├── scripts/
│   ├── deploy/
│   └── test/
├── zk-circuits/
└── docs/
```

**Action:** Create all directories upfront using:
```bash
mkdir -p contracts/{interfaces,core,libraries,deployment}
mkdir -p frontend/src/{components,hooks,config,utils}
mkdir -p frontend/public
mkdir -p scripts/{deploy,test}
mkdir -p zk-circuits docs
```

## Phase 2: Interface Contracts First

Define all interface contracts **before** implementing logic. This establishes clear API boundaries.

**Example interface structure:**
```solidity
// contracts/interfaces/IToken.sol
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
}
```

**Actions:**
1. List all external protocols you'll integrate (e.g., Aave, Connext, Uniswap)
2. Create interface files for each protocol's required functions
3. Create interfaces for your own contracts' public APIs
4. Validate interface completeness before proceeding

## Phase 3: Core Contract Implementation

Implement contracts in dependency order, starting with libraries, then core logic.

**Order of implementation:**
1. **Libraries** - Utility functions with no state dependencies
2. **Base contracts** - Abstract contracts with shared logic
3. **Core contracts** - Main business logic implementing interfaces
4. **Integration contracts** - Contracts that bridge multiple protocols

**Example:**
```solidity
// contracts/core/PrivatePool.sol
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "../interfaces/IToken.sol";
import "../libraries/SafeMath.sol";

contract PrivatePool {
    using SafeMath for uint256;
    
    IToken public token;
    mapping(address => uint256) private balances;
    
    constructor(address _token) {
        token = IToken(_token);
    }
    
    function deposit(uint256 amount) external {
        token.transferFrom(msg.sender, address(this), amount);
        balances[msg.sender] = balances[msg.sender].add(amount);
    }
}
```

**Actions:**
1. Implement one contract at a time
2. Compile and test each contract before moving to the next
3. Keep contracts focused on single responsibilities
4. Document all state variables and functions

## Phase 4: Frontend Configuration

Set up frontend infrastructure before building components.

**Create these config files first:**

1. **Network configuration** (`frontend/src/config/networks.js`):
```javascript
export const networks = {
  mainnet: {
    chainId: 1,
    rpcUrl: process.env.REACT_APP_MAINNET_RPC,
    explorer: 'https://etherscan.io'
  },
  testnet: {
    chainId: 5,
    rpcUrl: process.env.REACT_APP_TESTNET_RPC,
    explorer: 'https://goerli.etherscan.io'
  }
};
```

2. **Contract addresses** (`frontend/src/config/contracts.js`):
```javascript
export const contractAddresses = {
  PrivatePool: process.env.REACT_APP_PRIVATE_POOL_ADDRESS,
  Token: process.env.REACT_APP_TOKEN_ADDRESS
};
```

3. **ABI imports** (`frontend/src/config/abis/`):
```javascript
// Import compiled contract ABIs here
import PrivatePoolABI from './PrivatePool.json';
export { PrivatePoolABI };
```

**Actions:**
1. Create environment variable templates (`.env.example`)
2. Set up Web3 provider configuration (ethers.js, web3.js, or wagmi)
3. Configure wallet connection logic
4. Create utility functions for common operations

## Phase 5: Component Development

Build frontend components in this order:

1. **Utility components** - Buttons, inputs, modals (no Web3 logic)
2. **Web3 wrapper components** - Wallet connection, network switcher
3. **Contract interaction components** - Forms that call contract functions
4. **Data display components** - Components showing on-chain data
5. **Page components** - Assembling everything into pages

**Example component:**
```jsx
// frontend/src/components/DepositForm.jsx
import { useState } from 'react';
import { useContractWrite } from 'wagmi';
import { PrivatePoolABI } from '../config/abis';

export function DepositForm({ contractAddress, tokenAddress }) {
  const [amount, setAmount] = useState('');
  
  const { write: deposit } = useContractWrite({
    address: contractAddress,
    abi: PrivatePoolABI,
    functionName: 'deposit',
    args: [BigInt(amount)]
  });

  return (
    <form onSubmit={(e) => { e.preventDefault(); deposit(); }}>
      <input 
        value={amount} 
        onChange={(e) => setAmount(e.target.value)}
        placeholder="Amount"
      />
      <button type="submit">Deposit</button>
    </form>
  );
}
```

**Actions:**
1. Create components with clear prop interfaces
2. Implement error handling for all Web3 interactions
3. Add loading states for pending transactions
4. Test each component in isolation

## Phase 6: Deployment Scripts

Create deployment scripts last, after all contracts are complete and tested.

**Structure:**
```javascript
// scripts/deploy/01_deploy_token.js
async function main() {
  const Token = await ethers.getContractFactory("Token");
  const token = await Token.deploy();
  await token.deployed();
  console.log("Token deployed to:", token.address);
  
  // Save address for frontend config
  fs.writeFileSync(
    'frontend/.env',
    `REACT_APP_TOKEN_ADDRESS=${token.address}\n`,
    { flag: 'a' }
  );
}
```

**Actions:**
1. Create deployment scripts for each contract in dependency order
2. Include address saving mechanism for frontend integration
3. Create verification scripts for block explorers
4. Document deployment parameters and expected outputs

## Phase 7: Packaging and Delivery

**MANDATORY FINAL STEP** - Create a complete ZIP archive of the codebase before declaring the task complete. This phase must be executed even if iteration budget is limited.

**ZIP Creation Command:**
```bash
cd /path/to/project-root
zip -r project-name-deliverable.zip . -x "*.git*" -x "node_modules/*" -x "*.env"
```

**Packaging Checklist (must complete before task completion):**
- [ ] All contracts compiled and verified
- [ ] Frontend builds without errors
- [ ] Documentation is up to date
- [ ] ZIP file created and verified to contain:
  - [ ] All contract source files
  - [ ] All frontend source files
  - [ ] Deployment scripts
  - [ ] Configuration templates (.env.example)
  - [ ] README or setup instructions
- [ ] ZIP file is accessible and downloadable

**Actions:**
1. Navigate to project root directory
2. Create ZIP archive excluding git files, node_modules, and sensitive .env files
3. Verify ZIP contents with `unzip -l project-name-deliverable.zip`
4. Confirm ZIP file size is reasonable (>100KB for complete projects)
5. **BLOCKING**: Do not declare task complete until ZIP is created

## Key Principles

1. **Interface-first design** - Define contracts before implementing
2. **Layer-by-layer progression** - Complete each phase before next
3. **Incremental testing** - Test at each layer, not just at the end
4. **Configuration before components** - Set up infrastructure first
5. **Documentation at each step** - Update docs as you progress
6. **Packaging is mandatory** - Phase 7 must complete before task completion

## Recovery from Failures

If a tool failure occurs:
1. Note which phase completed successfully
2. Resume from the last completed phase
3. Re-validate interfaces and contracts before continuing
4. Use the directory structure as a progress checklist
5. **If iteration budget is low**, skip non-critical refinements and proceed directly to Phase 7 (Packaging) to ensure deliverable creation

## Iteration Budget Management

When working with limited iterations:
- **Phases 1-3 (Contracts)**: Allocate ~40% of iterations
- **Phases 4-5 (Frontend)**: Allocate ~40% of iterations
- **Phase 6 (Deployment)**: Allocate ~10% of iterations
- **Phase 7 (Packaging)**: **Reserve at least 2-3 iterations** - this is non-negotiable

If you reach 80% of iteration budget and Phase 7 has not started:
1. Stop adding new features
2. Complete minimum viable implementation
3. Execute ZIP creation immediately
4. Declare task complete with packaged deliverable

This systematic approach ensures partial progress is preserved, the overall architecture remains coherent, and a **complete deliverable artifact is always produced** even when interruptions occur.
