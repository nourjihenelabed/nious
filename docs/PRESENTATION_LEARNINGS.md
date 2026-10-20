# AXIOM Pricing Module — Track A
### What I understood, and what I did with it
**Nourjihen Elabed · Week 5 of 8**

Two halves. **Part 1** is the science — the problem each method solves and why
it's the right one. **Part 2** is the application — the same ideas as code.
Every application slide names the theory it implements.

Bullets are prompts. The **Say:** block is the content.

---

# PART 1 · THE SCIENCE
*What the problem actually is, and why these methods and not others.*

## 1 — Why price leakage is invisible

- Losses are **many and small**, not few and large
- Each concession is individually reasonable
- None of them appear together anywhere
- 15–20% of list price, industry-wide

**Say:** The reason companies lose a fifth of their price without noticing isn't incompetence — it's arithmetic. In B2B the losses aren't one big discount, they're twenty small concessions granted at different moments by different people: a volume rebate agreed in January, a freight allowance negotiated by logistics, an early-payment term set by finance, a co-op marketing credit from the campaign team, a side letter someone signed to close the quarter. Every one of them is defensible on its own. The problem is that no system adds them up, because they live on different documents and most never touch the invoice at all. So the loss is real and traceable, but only if you deliberately go and reconstruct it — which is what this module does.

## 2 — Data that carries its own meaning

- Graph: **concrete facts** — "what is happening"
- Ontology: **the concepts** — "what does it mean"
- Meaning lives on edges too, not just nodes
- Formal semantics → a machine can reason
- Complementary, not competing

**Say:** I spent real time on the distinction between a knowledge graph and an ontology, because the two get used interchangeably and they aren't the same. A knowledge graph holds concrete instance-level facts — this customer, this transaction, this product. An ontology is the conceptual layer above it: the classes, the constraints, the rules about what an entity *is*. The graph answers "what is happening"; the ontology answers "what does it mean". You need both, because the ontology is what lets two teams interpret the same data identically, and what lets a system infer something nobody wrote down — if every employee is a person, and Alice is an employee, a reasoner knows Alice is a person. In a property graph like Neo4j, the node and edge labels do a lightweight version of this job. The practical consequence for me is that meaning sits on the relationships as much as the entities: the quantity and unit price of a line item live on the edge between a transaction and a product, not in a table of their own.

## 3 — The waterfall is a forensic method, not a report

- Trace every euro from list to what we keep
- Each step is **a place money left**
- Off-invoice is the band nobody sees
- Fully deterministic — the certain ground

**Say:** The waterfall comes from a 1992 paper by Marn and Rosiello, and its insight is procedural rather than mathematical. You could just compute margin as revenue minus cost — one subtraction, done. The waterfall insists on doing it as a sequence, because then each step is a named place where value was given away, and "we lost 27 percent" becomes "twelve points went before we raised an invoice, twelve on the invoice, and three and a half off it." That's the difference between a number and a diagnosis. The classic finding is that the off-invoice band — the concessions that appear on no document a manager reads — is the bigger and less managed drain. And the whole thing is deterministic: no estimation, no model. That's why it's the right first thing to build, because everything after it depends on it being right.

## 4 — Segmentation on behaviour, not demographics

- Not size, not industry — **how they respond to price**
- Sensitivity · volume · margin · payment · mix
- Normalise first, or one feature dominates
- Stability is a requirement, not a bonus
- A cluster is a **proposal**, not a conclusion

**Say:** The instinct is to segment customers by what they are — enterprise, mid-market, SMB. That's a description, not a pricing input. What we actually need is grouping by how customers *behave* on price: how sensitive they are, what volume they take, what margin they contribute, how they pay, how varied their basket is. Two customers the same size can behave completely differently, and pricing should treat them differently. Methodologically it's k-means or hierarchical clustering, normalise features first so a large-numbered feature doesn't swamp the distance calculation, choose k with elbow and silhouette. But two requirements come from the governance side rather than the statistics. The assignments must be identical when re-run, because a segment that moves between runs cannot underpin a pricing policy anyone will sign. And a discovered cluster is a candidate for human ratification, not a conclusion — the algorithm proposes, a domain expert decides.

## 5 — Elasticity, and why B2B makes it hard

- β from `ln(Q) = α + β·ln(P)` **is** the elasticity
- **Endogeneity:** price responds to demand
- Thin segments → wild estimates
- Shrinkage: borrow strength from the category
- Interval always, point estimate never

**Say:** Elasticity is how much volume moves when price moves, and log-log regression is elegant because the coefficient is the elasticity directly. The difficulty isn't fitting it. It's that two things about B2B break the assumptions. First, endogeneity — prices aren't set at random, they're set in response to demand, so the regression picks up reverse causation and the raw coefficient is biased. Second, most segments are thin. If a segment has six deals, a naive regression will happily produce a confident-looking number that's essentially noise. The fix is hierarchical shrinkage, sometimes called partial pooling: you let each segment's estimate be pulled toward the category average in proportion to how little data it has. A segment with six hundred deals speaks for itself; a segment with six mostly gets told what the category thinks. The discipline that follows is that you never publish a point estimate — you publish an interval, and if that interval crosses zero or has an implausible sign, you say the estimate isn't fit to drive a price.

## 6 — The B2B demand curve is binary

- No smooth curve — each quote is **won or lost**
- So model **P(win | price, deal features)**
- Logistic acceptance model on historical quotes
- **Calibration** matters more than accuracy

**Say:** In retail you observe a demand curve, because thousands of people see a price and some fraction buy. In negotiated B2B you don't — you see a handful of quotes, and each one is simply won or lost. So the price-response function isn't a curve you measure, it's an acceptance probability you estimate: given this price relative to a reference, this deal size, this segment, this relationship, how likely are we to win? That's logistic regression on historical won and lost quotes. The thing I want to be careful about going into Week 6 is calibration rather than accuracy. Accuracy asks whether we predicted the right outcome; calibration asks whether "seventy percent" actually means seventy percent. The expected-profit calculation multiplies straight through that number, so a model that's directionally right but numerically overconfident produces confidently wrong prices.

## 7 — Optimisation under constraint

- Maximise **Π(p) = (p − c) · P(win|p)**
- Single-peaked between two bad extremes
- Floor · ceiling · minimum volume
- Grid search chosen **for auditability**
- No bandits, no RL — we cannot explore

**Say:** Expected profit is margin multiplied by the probability of winning, and the shape is intuitive: price too low and you always win but keep nothing; price too high and you keep a lot but rarely win. So there's a peak in between, and that peak is the recommendation — clipped to a floor at cost or policy, a ceiling at the market corridor, and a minimum volume constraint. The solver is a grid search over an explicit price ladder, and I want to be clear that's a deliberate choice, not a limitation. Because the acceptance curve is estimated rather than analytic, a transparent search over a visible ladder is auditable and trivially constrained, which a cleverer optimiser is not. And it's also why we don't use contextual bandits or reinforcement learning — those need to explore, meaning deliberately quote suboptimal prices to learn from the outcome. On sparse, high-value negotiated deals, exploring means losing a real deal on purpose. That's commercially unacceptable and impossible to defend to a client, so if we learn from history it has to be offline, counterfactually, never live.

## 8 — Governance is an engineering constraint

- "Declared, not inferred"
- Every number traceable to its records
- Abstention is a **valid output**
- Reproducibility is part of "done"

**Say:** This is the part that changed how I write code, so I want to be explicit about it. In most projects these would be nice principles. Here they're constraints that determine the design. "Declared, not inferred" means when I have to make a choice the data doesn't make for me, I state it and document it rather than burying it. "Traceable" means a number that can't name the records behind it isn't shippable. "Abstain" means the system needs a way to say *I don't know* that is as easy to produce as an answer — because if abstaining is harder than answering, nobody will do it under deadline. And reproducible means a result I can't regenerate isn't finished, because a client can come back in eighteen months and ask us to justify a specific price. Everything in Part 2 is one of these four made mechanical.

---

# PART 2 · THE APPLICATION

## 9 — The pipeline

> **Insert `pipeline_diagram.svg` full-bleed.**

- Knowledge graph → waterfall → segmentation → elasticity → optimiser → price
- Each stage **consumes the one before it**
- An error early propagates to everything after

**Say:** This is the whole track as one chain, and the dependency direction is the point. The waterfall needs the graph, because list price and unit cost and the discount amounts only exist there. Segmentation needs the waterfall, because we're clustering on pricing behaviour — realisation, leakage, margin contribution — and those are waterfall outputs; without it we'd be clustering on revenue, which tells us nothing about price. Elasticity needs segmentation, because a single global elasticity is meaningless and segments define both the groups β is estimated within and the hierarchy that shrinkage pools thin segments toward. And the optimiser needs elasticity, because without a demand response there's no curve to find a peak on. The consequence is that an error anywhere propagates to everything downstream — which is exactly why I refused to move past the waterfall until it was provably exact.

## 10 — Theory → application

| Theory | Became |
|---|---|
| Graph carries meaning on edges | `GraphSource` — one interface, two backends |
| Declared, not inferred | pro-rata documented as an assumption, one line to change |
| Abstain under uncertainty | three-state missing values; cross-currency refuses to publish |
| Traceable to records | `Evidence` IDs on every published record |
| Deterministic ground first | waterfall built and proved before any model |
| Reproducibility is "done" | snapshot hash linking MLflow to DVC |
| Shrinkage for thin segments | PyMC, Week 5–6 |

**Say:** I put this slide in because I didn't want the two halves to feel disconnected. Every row on the left is something I read about; every row on the right is a file in the repository. That mapping is really what I'd like to be judged on.

## 11 — Reading the graph without depending on it

- One `GraphSource` interface, two backends
- Swap CSV ↔ Neo4j with **one environment variable**
- Driver opens in `READ_ACCESS` — read-only at the connection
- Credentials hadn't arrived; the engine didn't wait

**Say:** Neo4j credentials hadn't been issued when I needed to start, and I decided that shouldn't block anything. So everything upstream talks to an abstract interface rather than to a database, with two implementations behind it — one reading a frozen CSV snapshot, one reading the live graph. Both return dataframes with identical column names and types, so nothing above them can tell the difference. The day the URI arrives, the change is one line in a config file; no engine code moves. And on the Neo4j side I open the driver session in read access mode, so read-only is enforced by the connection rather than by my own discipline — even a coding mistake physically cannot write to a client's graph.

## 12 — A test that reads its own source code

- The Cypher must be read-only
- So the test **greps the query file** for write clauses
- `CREATE` · `MERGE` · `SET` · `DELETE` · `DETACH` · `DROP`
- Proved mechanically, not promised in a comment

**Say:** This is a small thing I'm quite fond of. The promise that our queries never write to the client graph is the kind of thing that's usually a comment at the top of a file, and comments don't stop anyone. So instead the test opens the query module as text and scans it for every write clause in Cypher. If someone adds one, in six months, for a good reason, the suite goes red immediately. It costs three lines and it converts a promise into a property.

## 13 — One fact table, at line grain

- Compute **once**, at the finest grain
- Every aggregate is a `groupby` over it
- New dimension = one line, not a new pipeline
- No second calculation that can drift

**Say:** The obvious build is one calculation per question — margin by customer, then by product, then by segment. Six months later someone fixes a bug in one and forgets the others, and now two reports disagree with no way to tell which is right. So I compute everything exactly once at the finest grain available, the individual order line, into a single table, and every aggregate afterwards is a groupby over that same table. There's no second calculation to keep in sync. The payoff arrived immediately: margin by region crossed with month wasn't a feature, it was one line. And when I later fixed how missing costs were handled, every dimension became correct at once.

## 14 — Pro-rata: my one declared assumption

- Discounts attach to the **order**; my table is per **line**
- So I split them in proportion to line value
- **Exact in aggregate** — parts sum to the whole, tested
- Only an assumption at *product* grain

| Line | Value | Share | Of a 120 discount |
|---|---:|---:|---:|
| Product A | 800 | ⅔ | 80 |
| Product B | 400 | ⅓ | 40 |
| **Order** | **1,200** | **1** | **120** |

**Say:** The fact-table decision created a problem I had to solve honestly. Discounts in the graph attach to the whole transaction — there's no edge saying which line a rebate belongs to. But my table is at line grain, so I have to split it, and however I split it, that's my choice and not the data's. I allocate in proportion to line value. The important precision: this is exact in aggregate. The pieces sum back to the order total to the cent, and there's a test whose only job is to fail if they don't. So at transaction level and above — customer, segment, region, whole book — the choice makes no difference at all to the number. It only becomes an assumption at product grain.

## 15 — Where pro-rata breaks — and what replaces it

- Elastic + inelastic in one bundle
- A deliberate **loss leader**
- A mandatory add-on that got no discount
- Regional competitive differences

→ **Mitigate:** shrink extreme estimates; validate at portfolio level
→ **Replace:** infer the split from win/loss data (latent variable model)

**Say:** I wrote down where my own method fails, because I'd rather bring that than have it found. Four cases. In a bundle with one elastic and one inelastic product, the customer almost certainly negotiated on the elastic one, but I split by value. If one product is a deliberate loss leader priced low to move the other, I misread that completely. A mandatory add-on may have received no discount at all and I'll assign it one. And it ignores regional dynamics, where different products are competitive in different markets. My mitigations are that hierarchical shrinkage stops a mis-allocation producing a wild per-product elasticity, and that portfolio-level validation catches drift because the allocation cancels out in aggregate. The real fix, which I'd like to attempt, is inferring the actual per-product split from historical win/loss data with a latent variable model — turning my assumption into something learned from evidence. It needs more data and it's considerably more complex, so it's future work, not this week's.

## 16 — Missing is never zero — and there are three states

| State | Means | Stored as |
|---|---|---|
| No discount edge | genuinely no discount | **0**, stays certified |
| Edge, no amount | gave something, unrecorded | **unavailable** |
| Never populated | upstream gap | **unavailable** + reason |

- Zero a missing cost → reports 100% margin
- Zero an unknown discount → understates leakage

**Say:** Everyone knows not to treat missing as zero. What I didn't appreciate is that here there are three states, not two, and collapsing any pair produces a specific dangerous lie. A transaction with no discount edge genuinely had no discount — that's a real zero, it's our best deal, and it belongs in the average. A transaction that has discount edges but no recorded amount is completely different: we gave something away and don't know how much, and calling that zero understates leakage, which is the number the whole module exists to find. And a field never populated upstream is a data-quality problem I should report, not absorb. I got this wrong initially in the least obvious direction — I was reading full-price sales as "amount unknown" and dropping them out of the book, which quietly made average leakage look worse than it was. Distinguishing those three states is about five lines of code and it's the trickiest logic in the engine.

## 17 — Where I stopped trusting library defaults

- `sum(min_count=1)` returns a number if **any** row has one
- → half an order's cost, presented as the order's cost
- Replaced with a rule: sum only if **every** row has it
- Same for division: zero denominator → unavailable, never infinity

**Say:** This one changed how I read documentation. I had an order with two lines, one complete and one missing its cost. Pandas summed it and returned six hundred — the cost of one line out of two, presented as the cost of the whole order. And that's worse than a blank, because nothing about it looks wrong; against a thirteen-hundred pocket price it reads as a perfectly healthy margin. A blank makes you ask a question, a plausible wrong number doesn't. So I replaced the default with a rule: a step is summed only if every contributing row has it. I did the same for division — a free-of-charge order gave a margin percentage of negative infinity, which survives every null check I had, crossed the contract boundary, and would have serialised as invalid JSON that Track B literally could not load. Both defects came from the same reflex: reaching for the library's default instead of deciding what the absent case should mean.

## 18 — Building something to check myself against

- 11 transactions, by hand, round numbers
- Worked out **on paper before the engine saw them**
- Each pins one behaviour
- The oracle **checks itself** — dimensions must reconcile
- 279 comparisons, difference **0.00**

**Say:** I want to justify spending a week on tests instead of features. I already had two implementations to compare — the fast one and a naive loop-based one. If they agree, the arithmetic is right. Except they both read the same snapshot, so if I'd misunderstood what a column *meant*, both would agree and both would be wrong. Agreement between two things that share an assumption isn't evidence. So I built eleven transactions by hand and worked every step out on paper first. Each one pins one behaviour — a below-cost sale, a free order, a discount with no recorded amount, an order with one good line and one bad. I also made the expectations check themselves, so if *my* arithmetic slipped that fails before any engine test runs, which separates "the engine is broken" from "I can't add up". It found two real defects in code I thought was finished, and it's now the first stage of the pipeline, so a broken engine stops before it can produce a report.

## 19 — The contract, enforced by types

- `Money` is an object with a status — **not a float**
- You cannot read an amount without seeing if it exists
- Confidence carries **its reasons**, not just a score
- JSON serialiser **refuses** to emit NaN or Infinity
- Validator rejects "complete but cites nothing"

**Say:** The rule is that Track B explains only numbers the engine computed and never recalculates them. That's a rule in a document, and documents lose to deadlines. So I tried to make it enforced by the types. Money is an object carrying a status, so you physically cannot read an amount without also seeing whether that amount exists — missing and zero aren't confusable at the language level. Confidence carries the reasons behind it and not just a number, because Mohamed's narrative is required to *explain* low confidence, and it can only do that if I tell him why. The JSON serialiser is set to refuse NaN and Infinity outright, so a payload that would break on his side fails on mine instead. And a validator runs before anything is emitted and rejects records that claim to be complete while citing no evidence.

## 20 — MLOps: configuration as the product surface

- Engine settings live in **`pyproject.toml`**, not in code
- Environment variables override for deployment
- Optional extras: `[graph]` `[mlops]` `[models]` `[report]`
- MLflow degrades to a **no-op** if unavailable
- Snapshot **sha256** links an MLflow run to its DVC data

**Say:** Four things here. First, I moved every setting a host project might want to change — the allocation method, the discount taxonomy, the confidence thresholds, the currency policy, the minimum group size — out of the code and into `pyproject.toml`, with environment variables taking precedence for deployment. That means adopting this module is a config edit rather than a fork, which matters for where I want to take it. Second, the dependencies are split into optional extras, so a project that only wants the waterfall doesn't pull in PyMC and MLflow. Third, tracking never blocks a result: if MLflow isn't installed or the backend is down, the run degrades to a no-op object with the same interface and says so, because tracking failing should never be the reason a number can't be produced. And fourth, every run records a sha256 of the snapshot bytes — that hash is the join between MLflow and DVC, so given a run I can find the exact data and given the data I can regenerate the run.

## 21 — Applying segmentation — Week 5, in progress

- Features come from the **waterfall**, not the raw graph
- Realisation · leakage · margin contribution · order size · mix
- Compare k-means against hierarchical on the same features
- Prove stability by re-running with different seeds
- Gate it on the readiness score

**Say:** This is live on my board this week. The design decision I've already made is that the features come out of the waterfall rather than the raw graph — I'm clustering on price realisation, leakage rate and margin contribution, because those are pricing behaviour, whereas order count and revenue are just size. I'll fit both k-means and hierarchical on the same feature matrix and compare, because they fail differently and if they broadly agree that's evidence the structure is real rather than an artefact of the algorithm. Stability I'll demonstrate by re-running with different random seeds and checking assignments don't move. And I'm gating the whole thing on the readiness score, because clustering a book that can't report its own costs would produce confident, meaningless segments.

## 22 — Applying elasticity — Week 5, in progress

- Estimate β **within** segments, not globally
- Shrink thin segments toward the category
- Credible interval on every estimate
- Flag anything unfit to drive a price

**Say:** Elasticity is the piece I've deliberately left until I understood shrinkage properly, because it's the part I knew least. The plan is to estimate β within segments rather than globally, since a single elasticity across the whole book would average away exactly the differences we're trying to price on. Then shrinkage pulls the thin segments toward their category mean, so a segment with a handful of deals doesn't get to shout. Every estimate carries a credible interval, and any interval that crosses zero or has the wrong sign gets explicitly marked as unfit for optimisation — which means the optimiser downstream has to check that flag rather than blindly consuming a number.

## 23 — Applying win probability — PyMC, Week 6

- **PyMC** for the acceptance model, not plain logistic
- Bayesian gives me the **interval**, not just the estimate
- Same hierarchy: pool thin segments
- Check calibration before it feeds the optimiser
- Then grid search + business constraints

**Say:** This is where I intend to use PyMC. A plain logistic regression gives me a win probability but no honest sense of how sure it is, and the optimiser multiplies that probability straight through into expected profit — so an overconfident model produces overconfident prices. Fitting it Bayesianly gives me a posterior rather than a point, which means the recommended price can carry a real interval instead of a fabricated confidence score. It also lets me reuse the same hierarchical structure as the elasticity model, so segments with few historical quotes borrow strength from their category instead of producing noise. Before it feeds anything I'll check calibration — that when the model says seventy percent, it wins about seventy percent of the time. And only then does it go into the grid search over the price ladder with the floor, ceiling and volume constraints applied.

---

# PART 3 · WHERE I'M GOING

## 24 — Week 5 of 8

| Phase | Weeks | |
|---|---|---|
| Ramp-up | 1–2 | done |
| Core build I | 3–4 | done |
| **Core build II** | **5–6** | **← here** |
| Integration | 7–8 | ahead |

- Deterministic half: built and proved
- Statistical half: in progress

**Say:** The deterministic half is finished and defended. From here the discipline shifts from being right to being honest about how right I am.

## 25 — What I want to build next: make it an add-on

**The goal:** point this at *any* client's graph, not just this one

- **Declarative waterfall** — steps defined in config, not hardcoded
- **Schema mapping layer** — map a client's entities onto the pricing ontology
- **Readiness score as an admission test** — "can this module run on your data?"
- **Pip-installable, extras-based** — take only the surface you need
- **FastAPI + Docker** — callable as a service, not just runnable
- The result contract as a **stable public API**

**Say:** This is what I'd most like to work on beyond the plan. Right now the module is built for one graph with one shape. Almost every B2B company has a pricing leakage problem, and almost none of them have this exact schema — so the interesting question is how to make this a capability we can point at any client rather than a project we rebuilt each time.

> I've started: configuration already lives in `pyproject.toml` rather than in the code, and the data-access layer is already an interface with swappable backends.

**Say (continued):** Three more pieces would get it there. The waterfall steps themselves should be declared in config rather than hardcoded, because another company's concession types won't be ours — they might have distributor allowances or consignment costs we've never seen, and adding one shouldn't require touching the engine. Second, a schema mapping layer that maps a client's entity names onto our pricing ontology, so onboarding a new client is writing a mapping file rather than a fork. And third — this is the one I like most — the readiness score becomes an admission test. You point the module at a new dataset and it tells you honestly whether it can compute anything useful, and which specific fields are missing if it can't. That's the difference between a module and a product: a product knows when it doesn't apply.

## 26 — What I'd take away

- I came in able to train a model
- I'm leaving able to **defend a number**
- Best decision: build the oracle before more features
- Every defect came from writing down the expected answer

**Say:** Everything in my training up to now rewarded producing an output, and this project rewarded knowing when not to. The single best engineering decision I made was spending a week building something to check my own work against instead of adding features — and the pattern that proves it is that every defect I found came from writing down what the answer was supposed to be, never from re-reading code.

---

## Backup — likely questions

**"Isn't pro-rata just wrong?"**
> At transaction grain and above it makes no difference — the pieces sum back exactly, and that's tested. At product grain it's an assumption, it's the standard convention, and I've documented the four cases where it breaks. The proper fix is inferring the split from win/loss data.

**"Why not use `net_amount` from the graph?"**
> In this snapshot it nets *off*-invoice discounts, which is the reverse of what "net" conventionally means. Rather than guess which was intended, I ignore the field, compute from the discount edges, and pinned the observed behaviour in a test so we find out immediately if the real graph differs.

**"Why PyMC rather than scikit-learn?"**
> Because I need the interval, not just the estimate. The optimiser multiplies the win probability straight through into expected profit, so an overconfident model produces overconfident prices — and Bayesian fitting also gives me the hierarchical pooling I need for segments with few quotes.

**"What would you do differently?"**
> Build the reference dataset in Week 2 instead of Week 3. Every defect I found came from writing down the expected answer, and I could have been doing that from the first line of the waterfall.
