// Redline clause libraries. Ground truth for the AI reviewer: every flag either
// cites one of these entries or is marked as a general review.

export type Risk = 'high' | 'medium' | 'low';

export type ContractType =
  | 'athlete_nil'
  | 'creator'
  | 'entertainer'
  | 'employment'
  | 'terms_of_service'
  | 'rental'
  | 'mortgage'
  | 'nda'
  | 'services'
  | 'other';

export type Library = 'athlete_nil' | 'creator' | 'entertainer' | 'everyday' | 'rental' | 'mortgage' | 'general';

export interface ClauseEntry {
  id: string;
  library: Library;
  name: string;
  risk: Risk;
  exploit: string;
}

export const CONTRACT_TYPES: Record<ContractType, { label: string; libraries: Library[] }> = {
  athlete_nil: { label: 'Athlete / NIL deal', libraries: ['athlete_nil', 'general'] },
  creator: { label: 'Creator / brand deal', libraries: ['creator', 'general'] },
  entertainer: { label: 'Booking / entertainment deal', libraries: ['entertainer', 'general'] },
  employment: { label: 'Job offer / employment', libraries: ['everyday', 'general'] },
  terms_of_service: { label: 'Terms of service', libraries: ['everyday', 'general'] },
  rental: { label: 'Lease / rental agreement', libraries: ['rental', 'general'] },
  mortgage: { label: 'Mortgage / loan', libraries: ['mortgage', 'general'] },
  nda: { label: 'NDA', libraries: ['general'] },
  services: { label: 'Services / freelance agreement', libraries: ['general'] },
  other: { label: 'Contract', libraries: ['general'] },
};

export const CLAUSES: ClauseEntry[] = [
  // Athletes / NIL
  { id: 'nil_likeness_duration', library: 'athlete_nil', name: 'Image/likeness usage duration', risk: 'high', exploit: '"In perpetuity" language lets a brand use the athlete\'s image forever, including after they go pro elsewhere.' },
  { id: 'nil_exclusivity', library: 'athlete_nil', name: 'Exclusivity window', risk: 'high', exploit: 'Broad category exclusivity (e.g. "all footwear brands" instead of one competitor) blocks future deals.' },
  { id: 'nil_morality', library: 'athlete_nil', name: 'Morality/conduct clause', risk: 'medium', exploit: 'Vague "brings disrepute" language lets the brand exit for almost any reason, with no refund.' },
  { id: 'nil_minor_cosign', library: 'athlete_nil', name: 'Minor co-signature requirement', risk: 'high', exploit: 'Some state NIL laws require a parent/guardian signature; deals missing it can be unenforceable or legally risky.' },
  { id: 'nil_school_compliance', library: 'athlete_nil', name: 'School/conference compliance', risk: 'high', exploit: 'NCAA and state high-school association NIL rules vary; a deal that violates them can cost eligibility.' },
  { id: 'nil_payment_triggers', library: 'athlete_nil', name: 'Payment structure & triggers', risk: 'medium', exploit: 'Performance-based pay (e.g. "if you start 10 games") with vague measurement criteria.' },
  { id: 'nil_termination', library: 'athlete_nil', name: 'Termination clause', risk: 'medium', exploit: 'One-sided termination rights: the brand can exit anytime while the athlete is locked in.' },
  { id: 'nil_territory', library: 'athlete_nil', name: 'Territory/scope of use', risk: 'low', exploit: 'Local vs. national vs. global usage rights left undefined.' },
  { id: 'nil_approval_rights', library: 'athlete_nil', name: 'Approval rights over content', risk: 'low', exploit: 'The athlete gets no say in how their image or name is used in ads.' },

  // Content creators
  { id: 'cr_usage_duration', library: 'creator', name: 'Usage rights duration', risk: 'high', exploit: '"Perpetual, unlimited usage" instead of a defined window (e.g. 90 days).' },
  { id: 'cr_whitelisting', library: 'creator', name: 'Whitelisting / paid ad usage', risk: 'high', exploit: 'The brand runs the creator\'s content as paid ads without extra pay, often buried in boilerplate.' },
  { id: 'cr_platform_exclusivity', library: 'creator', name: 'Platform exclusivity', risk: 'medium', exploit: 'Broad "no other brand deals for 6 months" clauses that block income.' },
  { id: 'cr_deliverables', library: 'creator', name: 'Deliverable specificity', risk: 'medium', exploit: 'Vague deliverables ("some content") that balloon into unpaid extra posts.' },
  { id: 'cr_cross_channel', library: 'creator', name: 'Usage across the brand\'s other channels', risk: 'medium', exploit: 'Content licensed for one platform gets reused on the brand\'s global socials and website for free.' },
  { id: 'cr_ftc_disclosure', library: 'creator', name: 'FTC disclosure responsibility', risk: 'low', exploit: 'The contract is silent on who is liable if disclosure rules are not followed.' },
  { id: 'cr_kill_fee', library: 'creator', name: 'Kill fee / cancellation terms', risk: 'medium', exploit: 'No compensation if the brand cancels after the creator has already produced content.' },
  { id: 'cr_revisions', library: 'creator', name: 'Approval/revision rounds', risk: 'low', exploit: 'Unlimited free revisions eat unpaid time.' },
  { id: 'cr_payment_timing', library: 'creator', name: 'Payment timing', risk: 'medium', exploit: '"Net-90" or vague "upon campaign completion" payment triggers.' },

  // Entertainers
  { id: 'en_fee_structure', library: 'entertainer', name: 'Appearance/booking fee structure', risk: 'medium', exploit: 'Deposit vs. full payment terms unclear; no penalty for late or no payment.' },
  { id: 'en_likeness_promo', library: 'entertainer', name: 'Likeness use in promotional material', risk: 'high', exploit: 'Booking a one-time appearance but licensing the performer\'s image for ongoing marketing.' },
  { id: 'en_union', library: 'entertainer', name: 'Union/guild considerations', risk: 'high', exploit: 'Non-union terms for work that should be covered (e.g. SAG-AFTRA basics).' },
  { id: 'en_royalty', library: 'entertainer', name: 'Royalty/backend participation', risk: 'medium', exploit: 'Ambiguous "reasonable share of profits" language with no defined percentage.' },
  { id: 'en_cancellation', library: 'entertainer', name: 'Cancellation / kill fee', risk: 'medium', exploit: 'No compensation if the event or production cancels late.' },
  { id: 'en_travel', library: 'entertainer', name: 'Travel & expense terms', risk: 'low', exploit: 'Verbal promises about travel and expenses not written into the contract.' },
  { id: 'en_noncompete', library: 'entertainer', name: 'Non-compete / exclusivity', risk: 'medium', exploit: 'Overly broad restriction on similar bookings for extended periods.' },
  { id: 'en_merch', library: 'entertainer', name: 'Merchandising rights', risk: 'medium', exploit: 'Name or likeness on merchandise without separate compensation.' },

  // Everyday: job offers, performance letters, terms of service
  { id: 'ev_at_will', library: 'everyday', name: 'At-will employment language', risk: 'medium', exploit: 'Buried alongside verbal promises of job security that the contract does not actually guarantee.' },
  { id: 'ev_noncompete', library: 'everyday', name: 'Non-compete / non-solicit scope', risk: 'high', exploit: 'Overly broad geographic or time restrictions that outlast reasonable limits.' },
  { id: 'ev_arbitration', library: 'everyday', name: 'Arbitration clause', risk: 'high', exploit: 'Waives the right to sue or join a class action, often in dense boilerplate.' },
  { id: 'ev_severance', library: 'everyday', name: 'Severance terms & triggers', risk: 'medium', exploit: 'Vague "at company discretion" severance with no defined formula.' },
  { id: 'ev_ip_assignment', library: 'everyday', name: 'IP assignment clause', risk: 'high', exploit: '"Anything you create, even off-hours, belongs to the company" language.' },
  { id: 'ev_pip', library: 'everyday', name: 'Performance Improvement Plan terms', risk: 'medium', exploit: 'Vague, unmeasurable goals designed to justify termination rather than support improvement.' },
  { id: 'ev_data_usage', library: 'everyday', name: 'Data usage / privacy', risk: 'medium', exploit: 'Broad rights to sell or share user data buried in a long agreement.' },
  { id: 'ev_auto_renewal', library: 'everyday', name: 'Auto-renewal / cancellation friction', risk: 'medium', exploit: 'Silent auto-renewal and deliberately hard cancellation processes.' },
  { id: 'ev_liability_waiver', library: 'everyday', name: 'Liability waiver scope', risk: 'medium', exploit: 'Overly broad waiver of the company\'s own liability, even for negligence.' },

  // Housing: rentals
  { id: 're_deposit', library: 'rental', name: 'Security deposit terms', risk: 'high', exploit: 'Vague deduction criteria and no defined return timeline, a common source of disputes.' },
  { id: 're_rent_increase', library: 'rental', name: 'Rent increase / escalation', risk: 'high', exploit: 'Uncapped increases or minimal notice periods buried in renewal language.' },
  { id: 're_early_termination', library: 'rental', name: 'Early termination / break clause', risk: 'medium', exploit: 'Excessive penalty fees or unclear notice requirements.' },
  { id: 're_maintenance', library: 'rental', name: 'Maintenance & repair responsibility', risk: 'medium', exploit: 'Shifting normal wear-and-tear repair costs onto the tenant.' },
  { id: 're_holdover', library: 'rental', name: 'Automatic renewal (holdover)', risk: 'medium', exploit: 'Silent auto-renewal into a full new lease term instead of month-to-month.' },
  { id: 're_late_fees', library: 'rental', name: 'Late fee structure', risk: 'medium', exploit: 'Excessive or compounding late fees beyond what state law allows.' },
  { id: 're_entry', library: 'rental', name: 'Landlord entry/access rights', risk: 'low', exploit: 'Vague "reasonable notice" language enabling frequent or short-notice entry.' },
  { id: 're_habitability', library: 'rental', name: 'Habitability disclosures', risk: 'high', exploit: 'Language that tries to waive the tenant\'s right to habitable conditions (often unenforceable, but intimidating if signed).' },
  { id: 're_sublet', library: 'rental', name: 'Subletting/assignment rights', risk: 'low', exploit: 'Outright bans that limit the tenant\'s flexibility without clear justification.' },

  // Housing: mortgages
  { id: 'mo_arm', library: 'mortgage', name: 'Interest rate adjustment terms (ARM)', risk: 'high', exploit: 'A low "teaser" rate resets sharply higher later, with unclear rate caps.' },
  { id: 'mo_prepayment', library: 'mortgage', name: 'Prepayment penalty', risk: 'medium', exploit: 'A fee for paying off the loan early, often buried in boilerplate.' },
  { id: 'mo_balloon', library: 'mortgage', name: 'Balloon payment clause', risk: 'high', exploit: 'A large lump-sum payment due at term end that borrowers are not prepared for.' },
  { id: 'mo_acceleration', library: 'mortgage', name: 'Default & acceleration clause', risk: 'high', exploit: 'A minor missed payment can make the full loan balance due immediately.' },
  { id: 'mo_escrow', library: 'mortgage', name: 'Escrow account terms', risk: 'medium', exploit: 'Tax and insurance shortfalls or mismanagement passed on to the borrower with little transparency.' },
  { id: 'mo_pmi', library: 'mortgage', name: 'PMI removal conditions', risk: 'medium', exploit: 'Unclear or delayed conditions for cancelling private mortgage insurance once eligible.' },
  { id: 'mo_due_on_sale', library: 'mortgage', name: 'Due-on-sale clause', risk: 'medium', exploit: 'The loan becomes fully due if the property is transferred, even in some family situations.' },
  { id: 'mo_closing_costs', library: 'mortgage', name: 'Closing cost itemization', risk: 'medium', exploit: 'Junk fees bundled into closing costs without a clear breakdown.' },

  // General: applies to every contract
  { id: 'gen_perpetuity', library: 'general', name: 'Perpetual or irrevocable rights', risk: 'high', exploit: 'Grants or obligations that never end.' },
  { id: 'gen_auto_renewal', library: 'general', name: 'Auto-renewal', risk: 'medium', exploit: 'The contract rolls over unless a short cancellation window is caught.' },
  { id: 'gen_indemnity', library: 'general', name: 'Unlimited indemnity', risk: 'high', exploit: 'The signer pays for the other side\'s losses with no ceiling, even ones they cause.' },
  { id: 'gen_unilateral_changes', library: 'general', name: 'One-sided changes', risk: 'medium', exploit: 'The other side can rewrite the deal after signing.' },
  { id: 'gen_noncompete', library: 'general', name: 'Broad non-compete', risk: 'high', exploit: 'Restricts where and for whom the signer can work.' },
  { id: 'gen_ip_grab', library: 'general', name: 'IP grab', risk: 'high', exploit: 'Takes ownership of work beyond the project, sometimes the signer\'s prior work.' },
  { id: 'gen_penalty', library: 'general', name: 'Penalty / liquidated damages', risk: 'medium', exploit: 'Fixed payouts that can exceed any real loss.' },
  { id: 'gen_termination', library: 'general', name: 'Lopsided termination', risk: 'medium', exploit: 'Only one side can walk away freely.' },
  { id: 'gen_liability_shield', library: 'general', name: 'Liability shield', risk: 'medium', exploit: 'Caps or removes the other side\'s liability but not the signer\'s.' },
  { id: 'gen_arbitration', library: 'general', name: 'Forced arbitration', risk: 'high', exploit: 'Removes the right to court and to join a class action.' },
  { id: 'gen_venue', library: 'general', name: 'Remote venue / governing law', risk: 'low', exploit: 'A dispute means litigating far from home under unfamiliar law.' },
  { id: 'gen_confidentiality', library: 'general', name: 'Endless or one-way confidentiality', risk: 'low', exploit: 'Secrecy obligations that never expire and bind only the signer.' },
  { id: 'gen_late_fees', library: 'general', name: 'Punitive late fees / interest', risk: 'medium', exploit: 'Fees or interest that compound fast.' },
  { id: 'gen_assignment', library: 'general', name: 'Free assignment', risk: 'low', exploit: 'The signer can end up contracted to a stranger.' },
];

export const CLAUSE_BY_ID = new Map(CLAUSES.map(c => [c.id, c]));

export const RISK_WEIGHT: Record<Risk, number> = { high: 18, medium: 10, low: 5 };
export const BASE_RISK = 12;
