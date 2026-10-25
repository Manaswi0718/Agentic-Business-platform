# Udyam Compliance Engine — Indian small-business registration rules

![tag:rules-engine](https://img.shields.io/badge/rules-deterministic-blue) ![tag:msme](https://img.shields.io/badge/MSME-GST-FSSAI-orange) ![tag:chat-protocol](https://img.shields.io/badge/chat-protocol-green)

A deterministic rules engine that turns a small-business profile into the exact list of Indian registrations it needs, each with official portal, government fee, documents, deadline, penalty and source.

Covers: Udyam (MSME, limits from 1 Apr 2025), GST (state-wise ₹40/20/10 lakh thresholds, inter-state and e-commerce compulsory registration, CGST s.22/24), FSSAI (thresholds revised 1 Apr 2026: Basic Registration up to ₹1.5 crore), Shops & Establishments, municipal trade licence, Legal Metrology (scale stamping, packaged labels), ESIC (10+ staff), EPFO (20+ staff), trademark.

Also checks whether a website is an official government portal, flagging paid look-alike sites.

## Use it
- Chat: fill the form card, or paste a link ("is udyamregistration.co.in official?").
- Agents: send `ComplianceRequest{request_id, kind: "plan" | "check_link", payload_json}` and receive `ComplianceResponse{request_id, ok, result_json}`.

Rules version 2026-10-24. Not legal advice.
