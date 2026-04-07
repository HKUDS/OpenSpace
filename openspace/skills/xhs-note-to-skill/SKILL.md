---
name: xhs-note-to-skill
description: "Convert Xiaohongshu (小红书 / REDnote) how-to notes into structured, self-evolving OpenSpace skills. Xiaohongshu users share step-by-step walkthroughs of online services — tax filing, visa applications, insurance claims, government portals, SaaS onboarding, etc. — complete with screenshots, pitfall warnings, and workarounds for broken UIs. These are exactly the procedural knowledge that agents need to automate online workflows. Use when: (1) given a Xiaohongshu note URL or content about an online service to convert into a skill, (2) asked to find Xiaohongshu tutorials for a specific online task, (3) batch-converting service walkthroughs into an agent skill library."
---

# XHS Note-to-Skill: Online Service Workflows

Convert procedural knowledge from Xiaohongshu (小红书) into structured, evolvable skills for automating online services.

## Why Xiaohongshu for Online Service Skills?

When people in China encounter a confusing government portal, a broken insurance claim form, or a multi-step tax filing process, they don't read official documentation — they search Xiaohongshu (小红书). The platform has become China's de facto **crowdsourced manual for online services**.

What makes these notes valuable as skill sources:

- **Procedure-first**: Users document exact click paths, form fields, and UI states — not concepts, but executable steps
- **Failure-mode rich**: The most-saved notes are the ones that say "当你看到这个报错别慌" (when you see this error, don't panic) — exactly the error-handling logic agents need
- **Workaround-heavy**: Official docs say "click Submit." Xiaohongshu notes say "click Submit, wait 30 seconds, if nothing happens clear cache and try on Chrome, the WeChat browser doesn't work"
- **Screenshot-documented**: Users embed annotated screenshots of every step — UI state verification built-in
- **Continuously patched**: When a government portal updates its UI, someone posts "2026年新版界面" within days, and old notes get flagged in comments

This is the **exact type of knowledge** that OpenSpace's benchmark tasks test — tax returns, compliance forms, payroll calculations — but sourced from real users instead of synthetic datasets.

## Note Anatomy → Skill Mapping

Online service notes on Xiaohongshu follow a consistent structure:

| Note Element | Skill Component | Why It Matters for Agents |
|---|---|---|
| 平台/入口 Platform & entry point | **Prerequisites** | Which app/website/mini-program to use |
| 操作步骤 Step-by-step screenshots | **Execution Steps** | Exact click paths, form values, wait times |
| 避坑/踩雷 Pitfalls & errors | **Error Handling** | Known failure modes + workarounds |
| 所需材料 Required documents | **Input Validation** | What to prepare before starting |
| 时间节点 Deadlines & timing | **Scheduling Constraints** | "Must apply between March 1-June 30" |
| 评论区更新 Comment updates | **Evolution Signal** | "2026 version changed, now click here instead" |
| 到账时间 Processing time | **Expected Output** | "Refund arrives in 5-10 business days" |

## Conversion Workflow

### Phase 1: Extract

Given a note about an online service:

1. **Identify the service type**: tax / social security / visa / insurance / banking / government portal / SaaS setup
2. **Extract procedural elements**:
   - **Service**: What platform and which specific function
   - **Prerequisites**: Required accounts, documents, pre-conditions
   - **Steps**: Ordered click-path with UI state descriptions
   - **Decision Points**: "If you see X, do A; if you see Y, do B"
   - **Error States**: Known errors and their workarounds
   - **Timing**: Deadlines, processing windows, expected completion
   - **Output**: What success looks like (confirmation page, reference number, deposit)
3. **Assess reliability signals**:
   - Saves count (utility indicator)
   - Screenshot quality (step coverage)
   - Comment corrections (version drift indicator)
   - Post date vs. service's last known UI update

### Phase 2: Structure as SKILL.md

```markdown
---
name: <service>-<specific-task>-<region>
description: "<what this skill automates, in English and Chinese>"
tags: [xiaohongshu, online-service, <service-type>, chinese]
source_platform: xiaohongshu
source_engagement: <saves_count>
language: zh-CN
service_url: <primary platform URL>
last_verified: <YYYY-MM>
---

# <Service Task Name>

## Goal
<What this skill accomplishes — specific, measurable outcome>

## Prerequisites
- [ ] Account: <which platform, registration requirements>
- [ ] Documents: <ID, bank statements, etc.>
- [ ] Timing: <filing windows, deadlines>
- [ ] Device: <specific browser/app requirements>

## Steps

### Step 1: <Action>
- Navigate to: <exact URL or app path>
- UI state: <what you should see>
- Action: <what to click/fill>
- ⚠️ Known issue: <if any>

### Step 2: <Action>
...

## Decision Points
- IF <condition A> → follow Step X
- IF <condition B> → follow Step Y
- IF <error message> → see Error Handling

## Error Handling
| Error | Cause | Workaround |
|-------|-------|------------|
| <error text> | <why it happens> | <how to fix> |

## Expected Output
- Confirmation: <what success looks like>
- Timeline: <processing time>
- Verification: <how to check status>

## Version History
- <YYYY-MM>: Current version (based on note posted <date>)
- Known upcoming changes: <if any>
```

### Phase 3: Add Evolution Metadata

Tag elements that drive OpenSpace self-evolution:

1. **Breakpoint markers** `[FRAGILE]`: Steps most likely to break when the service updates its UI
2. **Verification queries**: How to check if a step is still valid (e.g., "screenshot the current UI and compare")
3. **Alternative paths**: Different approaches from comment section ("I did it through the WeChat mini-program instead, faster")

## Example: China Individual Tax Refund (个税退税)

### Source: Xiaohongshu Note (anonymized, ~15K saves)

> **2025年度个税退税保姆级教程｜手把手教你退钱💰**
>
> 退税时间：2026年3月1日-6月30日
> 提前预约：2月25日起可预约3月1日-20日的名额
>
> Step 1: 下载"个人所得税"APP → 实名认证
> Step 2: 首页 → 2025年度综合所得年度汇算
> Step 3: 选择"我需要申报表预填服务" → 开始申报
> Step 4: 核对收入（工资薪金、劳务报酬等）
> Step 5: 确认专项附加扣除（房贷/租房/子女教育/赡养老人）
> Step 6: 提交申报 → 选择退税银行卡
>
> 🔴避坑：
> - 一定要选"预填服务"！！！自己填容易漏掉收入被罚
> - 专项扣除很多人漏填了→白白少退几千块
> - 银行卡必须是I类账户，不是所有卡都行
> - 3月初系统巨卡，建议3月下旬再申报
> - 劳务报酬有些单位没报，要自己手动添加

### Output: SKILL.md

```markdown
---
name: china-individual-tax-refund
description: "File annual individual income tax reconciliation and claim refund via China's IIT APP (个人所得税APP). Covers the complete workflow from app setup to refund receipt. 中国个人所得税年度汇算退税全流程。"
tags: [xiaohongshu, online-service, tax, china, government]
source_platform: xiaohongshu
source_engagement: 15000
language: zh-CN
service_url: https://etax.chinatax.gov.cn
last_verified: 2026-03
---

# China Individual Income Tax Annual Refund (个税年度汇算退税)

## Goal
Complete 2025 annual tax reconciliation (年度综合所得汇算清缴) and receive tax refund via the official IIT mobile app.

## Prerequisites
- [ ] "个人所得税" APP installed (iOS App Store / Android app markets)
- [ ] Real-name authentication completed (requires national ID)
- [ ] Class I bank account (I类银行卡) linked — not all bank cards qualify
- [ ] Filing window: March 1 – June 30, 2026
- [ ] Optional: Pre-appointment for March 1-20 (opens Feb 25)

## Steps

### Step 1: App Setup & Authentication
- Download "个人所得税" from official app store
- Complete facial recognition + ID verification
- Bindbank card under "我的 → 银行卡"
- ⚠️ Must be a Class I account (一类户). Check with your bank if unsure

### Step 2: Start Filing
- Home screen → tap "2025年度综合所得年度汇算"
- Select "我需要申报表预填服务" (pre-filled form service) [FRAGILE]
- ⚠️ CRITICAL: Always use pre-filled service. Manual entry risks omitting income sources — the tax authority may flag this as underreporting

### Step 3: Verify Income Sources
- Review pre-filled income: 工资薪金 (salary), 劳务报酬 (freelance), 稿酬 (royalties), 特许权使用费 (licensing fees)
- ⚠️ Some employers/clients fail to report payments. If you know you earned freelance income that's missing → manually add it under 劳务报酬
- Cross-check against your bank statements for completeness

### Step 4: Confirm Deductions (专项附加扣除) [FRAGILE]
- Review each category — many people miss eligible deductions:
  - 子女教育 Children's education (1000/month per child)
  - 继续教育 Continuing education
  - 住房贷款利息 Mortgage interest (1000/month)
  - 住房租金 Rent (800-1500/month depending on city)
  - 赡养老人 Elderly parent support (3000/month)
  - 大病医疗 Major medical expenses
  - 婴幼儿照护 Infant care (2000/month per child, since 2023)
- ⚠️ This is where most refund money comes from. Missing deductions = leaving money on the table

### Step 5: Submit & Select Refund Account
- Review calculated refund/payment amount
- IF refund → select linked bank card → submit
- IF payment due → can pay directly through app
- Receive confirmation number (申报编号)

### Step 6: Track Refund
- 首页 → 申报记录 → check status
- Typical processing: 5-10 business days
- Refund deposited to linked bank card

## Decision Points
- IF pre-filled data is empty → wait 1-2 days (data sync delay from employers)
- IF refund amount seems too low → check Step 4 deductions thoroughly
- IF app shows "系统繁忙" → try again after 10 PM or wait until late March
- IF bank card rejected → verify it's a Class I account with your bank

## Error Handling
| Error | Cause | Workaround |
|---|---|---|
| 系统繁忙/服务器忙 | Peak traffic (early March) | Try after 10 PM or wait until March 21+ |
| 银行卡校验失败 | Non-Class-I account | Switch to a major bank Class I card |
| 收入数据为空 | Employer hasn't filed yet | Wait 2-3 days, or contact employer's HR |
| 申报表校验不通过 | Missing required fields | Check all deduction forms are fully filled |
| 退税金额为0 | Tax already settled monthly | Normal — means your employer withheld correctly |

## Expected Output
- Confirmation page with 申报编号 (filing number)
- Refund deposited within 5-10 business days
- Status trackable in app under 申报记录

## Timing
- Filing window: 2026-03-01 to 2026-06-30
- Pre-appointment: Opens 2026-02-25 for March 1-20 slots
- Best time to file: After March 21 (no appointment needed, less server load)
- ⚠️ Miss the June 30 deadline = lose your refund for this year

## Version History
- 2026-03: Current version. APP UI largely unchanged from 2025.
- Known changes: 婴幼儿照护 deduction amount increased in 2024, verify current limits
```

## Supported Service Domains

| Domain | Chinese Term | Example Skills | Xiaohongshu Coverage |
|---|---|---|---|
| Tax Filing | 报税/退税 | `china-individual-tax-refund`, `freelancer-tax-quarterly` | Very high (seasonal surge every March) |
| Social Security | 社保 | `social-security-transfer-city`, `maternity-benefit-claim` | High |
| Housing Fund | 公积金 | `housing-fund-withdrawal-rental`, `housing-fund-loan-apply` | High |
| Visa & Immigration | 签证 | `japan-tourist-visa-apply`, `us-b1b2-ds160-fill` | Very high |
| Insurance Claims | 保险理赔 | `car-insurance-claim-online`, `medical-reimbursement-submit` | Medium-high |
| Banking | 银行业务 | `bank-account-type-upgrade`, `cross-border-transfer-guide` | Medium |
| Government Portals | 政务服务 | `birth-certificate-apply-online`, `business-license-register` | High |
| SaaS Onboarding | 工具教程 | `notion-workspace-setup`, `feishu-approval-workflow` | Medium |

## Quality Thresholds

Not all notes are worth converting. For **online service** skills:

| Signal | Threshold | Rationale |
|---|---|---|
| 收藏 Saves | ≥ 1,000 | Service skills need higher bar — stakes are real (money, legal) |
| 步骤 Steps | ≥ 5 distinct steps | Fewer steps = too simple to need a skill |
| 截图 Screenshots | ≥ 3 | Screenshot = UI state verification = agent can validate |
| 避坑 Error cases | ≥ 2 | Error handling is the core value vs. official docs |
| 时效 Recency | ≤ 6 months | Service UIs change frequently |

## Evolution Mechanics

This is where the XHS → OpenSpace pipeline creates compounding value:

### FIX Trigger (Auto-repair)
- Government portal updates UI → skill step fails → OpenSpace searches for "2026新版" notes on the same service → patches the broken step
- Example: 个税APP redesigns the deduction page every January → new Xiaohongshu notes appear within a week → FIX trigger pulls updated click paths

### DERIVED Trigger (Variant generation)
- Base skill: `china-individual-tax-refund` (standard employee)
- Auto-derived: `china-tax-refund-freelancer` (multiple income sources)
- Auto-derived: `china-tax-refund-stock-income` (capital gains reporting)
- Source: Related notes recommended by Xiaohongshu's algorithm

### CAPTURED Trigger (Learning from execution)
- Agent executes the tax refund skill but encounters an unlisted error
- Agent improvises a workaround (e.g., switching browsers)
- OpenSpace captures the successful workaround as a new error-handling rule
- Next time any agent hits the same error → already handled

## Integration with OpenSpace Cloud

Converted skills can be uploaded to the OpenSpace community:

```bash
# Upload a single converted skill
openspace upload --skill-dir ./skills/china-individual-tax-refund

# Batch upload a domain pack
openspace upload --skill-dir ./skills/ --tag "chinese-gov-services"
```

Community benefits:
- One user converts a high-quality 个税退税 note → all agents gain tax filing capability
- Multiple users convert notes for the same service → OpenSpace merges into a best-of-breed skill
- Seasonal updates (e.g., new tax year) trigger community-wide skill refresh
