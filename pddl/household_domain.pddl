;; ─────────────────────────────────────────────────────────────────────
;; household_domain.pddl
;;
;; FIXED, hand-authored PDDL domain for the LLM+P baseline.
;;
;; The operators mirror, one-to-one, the ROBOT_ACTIONS vocabulary and the
;; precondition / effect semantics enforced by pyplanner/verifier.py
;; (SymbolicState: arrived, found, holding, opened, on).  The point is that
;; LLM+P (classical-planner hybrid) and SAGE (rule verifier) are
;; grounded in the SAME world model, so the comparison is fair.
;;
;; Predicate <-> SymbolicState field mapping:
;;   (arrived ?l)        <-> state.arrived == ?l           (robot at location/handle)
;;   (found ?o)          <-> state.found   == ?o           (last Find target)
;;   (holding ?o)        <-> state.holding == ?o           (object in gripper)
;;   (hand-empty)        <-> state.holding is None
;;   (opened ?c)         <-> ?c in state.opened            (container is open)
;;   (turned-on ?a)      <-> ?a in state.on                (appliance powered)
;;
;; STRIPS only (typing + negative preconditions) so that pure-python
;; planners (pyperplan: blind/bfs/wastar + lmcut/hadd/hmax heuristics) and
;; classical planners alike can solve it without ADL features.
;;
;; NOTE: this domain is intentionally PERMISSIVE on what counts as a
;; "location" vs an "object" vs a "container": every constant is declared
;; with type `obj` in the problem file, and the same constant can be used as
;; a MoveTo destination AND a Find target.  This matches the verifier, which
;; does not maintain a typed ontology either — it only tracks the 5 fields
;; above.  Keeping the domain untyped-ish avoids LLM type-assignment errors,
;; the single biggest source of unsolvable LLM-authored problems (risk R1).
;; ─────────────────────────────────────────────────────────────────────
(define (domain household)
  (:requirements :strips :typing :negative-preconditions)

  (:types
    obj - object        ;; every constant (room, furniture, item, container) is an obj
  )

  (:predicates
    (arrived   ?l - obj)   ;; robot has navigated to ?l
    (found     ?o - obj)   ;; robot has localised ?o (Find)
    (holding   ?o - obj)   ;; ?o is in the gripper
    (hand-empty)           ;; gripper is free
    (opened    ?c - obj)   ;; container ?c is open
    (turned-on ?a - obj)   ;; appliance ?a is on
  )

  ;; ── MoveTo <loc> : navigate; sets arrived, clears any stale found ──
  ;; Verifier: MoveTo sets state.arrived = o; if found and found != o, found = None.
  ;; We model the common case: moving forgets the previously-found handle.
  ;; (Find before any interaction re-establishes `found`, exactly as the
  ;;  verifier requires.)
  (:action moveto
    :parameters (?to - obj ?from - obj ?f - obj)
    :precondition (and (arrived ?from) (found ?f))
    :effect (and (arrived ?to) (not (arrived ?from))
                 (not (found ?f)))
  )

  ;; MoveTo when nothing is currently found (no found-fluent to clear).
  (:action moveto-nofound
    :parameters (?to - obj ?from - obj)
    :precondition (and (arrived ?from))
    :effect (and (arrived ?to) (not (arrived ?from)))
  )

  ;; ── Find <obj> : localise a target; sets found ──
  ;; Verifier: Find requires a non-empty object.  We require `arrived` at SOME
  ;; location to keep plans grounded, which every real init provides.
  (:action find
    :parameters (?o - obj ?at - obj)
    :precondition (and (arrived ?at))
    :effect (and (found ?o))
  )

  ;; ── Pick : requires found target + empty hand; sets holding, clears found ──
  ;; Verifier: Pick requires not holding and state.found set; holds last-found,
  ;; then found = None.
  (:action pick
    :parameters (?o - obj)
    :precondition (and (found ?o) (hand-empty))
    :effect (and (holding ?o) (not (hand-empty)) (not (found ?o)))
  )

  ;; ── Place <recep> : requires holding + arrived==recep; frees hand ──
  ;; Verifier: Place requires holding and state.arrived == receptacle.
  (:action place
    :parameters (?o - obj ?recep - obj)
    :precondition (and (holding ?o) (arrived ?recep))
    :effect (and (hand-empty) (not (holding ?o)))
  )

  ;; ── PutIn <container> : requires holding + container found + open ──
  ;; Verifier: PutIn requires holding and container open (when a known container).
  (:action putin
    :parameters (?o - obj ?c - obj)
    :precondition (and (holding ?o) (found ?c) (opened ?c))
    :effect (and (hand-empty) (not (holding ?o)))
  )

  ;; ── Open <container> : requires found; sets opened ──
  ;; Verifier: Open/Close/TurnOn/TurnOff require found(target) (or arrived==target).
  (:action open
    :parameters (?c - obj)
    :precondition (and (found ?c))
    :effect (and (opened ?c))
  )

  ;; ── Close <container> : requires found + currently opened; clears opened ──
  (:action close
    :parameters (?c - obj)
    :precondition (and (found ?c) (opened ?c))
    :effect (and (not (opened ?c)))
  )

  ;; ── TurnOn <appliance> : requires found; sets turned-on ──
  (:action turnon
    :parameters (?a - obj)
    :precondition (and (found ?a))
    :effect (and (turned-on ?a))
  )

  ;; ── TurnOff <appliance> : requires found + currently on; clears turned-on ──
  (:action turnoff
    :parameters (?a - obj)
    :precondition (and (found ?a) (turned-on ?a))
    :effect (and (not (turned-on ?a)))
  )
)
