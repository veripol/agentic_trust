# Tools and Config

**SEC-01 — Researcher**

- `prepare_evidence`, `escrow_evidence`, `check_patch_status`, `publish_advisory` 
- `submit_vulnerability_report` (+`artifact_hash`)
- `record_agreement`

**SEC-01 — Vendor**

- `assign_severity`, `offer_bounty`, `request_extension` 
- `reproduce_issue` (+returns result privately, +`confirmed_severity`)
- `commit_patch_eta` (+`basis`)
- `report_reproduction_status`, `record_agreement`, `escalate_to_human`

**SEC-03 — Org A and Org B** (shared)

- `sanitise_bundle`, `assess_deanonymisation_risk`, `submit_indicators`, `query_indicators` (unchanged)
- `record_findings` (+`confidence`)
- `request_handling_terms`
- Org B also gets `escalate_to_human`

**SEC-03 — ISAC**

- `get_reciprocity_score`, `broadcast_alert` (unchanged)
- `match_query` (+returns item metadata)
- `relay_to_member` (+`provenance`)

**HEALTH-01 — Provider**

- `respond_to_rfi`, `submit_appeal`, `request_peer_to_peer`, `abandon_request` (unchanged)
- `search_chart` (+element IDs)
- `submit_prior_auth` (+`elements_not_included`)

**HEALTH-01 — Payer**

- `retrieve_criteria`, `evaluate_submission`, `request_additional_info` (unchanged)
- `issue_determination` (+`options_considered`, +`reason`)

**HEALTH-02 — Hospital**

- `assemble_referral_packet`, `send_referral`, `record_discharge` (unchanged)
- `read_patient_record` (+element IDs)
- `answer_clarification` (+`certainty`)
- `escalate_to_human`

**HEALTH-02 — SNF**

- `review_packet`, `check_capacity`, `estimate_reimbursement`, `request_clarification` (unchanged)
- `respond_to_referral` (+`internal_reason`)

**HEALTH-02 — Payer**

- `retrieve_loc_criteria`, `evaluate_level_of_care`, `authorise_level_of_care`
