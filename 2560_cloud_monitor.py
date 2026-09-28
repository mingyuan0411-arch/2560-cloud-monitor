
        if not contract:
            print(f"{base:<5} NOT_FOUND")
            results.append({
                "base": base,
                "status": "NOT_FOUND",
            })
            continue

        try:
            r = analyze(base, contract)
            results.append(r)

            if r.get("status") == "WAIT_HISTORY":
                print(
                    f"{base:<5} WAIT_HISTORY "
                    f"4H={r.get('history_4h_bars')} "
                    f"1D={r.get('history_1d_bars')}"
                )
                notify_status_change(r, state)
                time.sleep(0.15)
                continue

            g = r.get("grid") or {}
            grid_text = ""
            if r["status"] in ("PRE-STRICT", "STRICT"):
                grid_text = (
                    f" grid={price_text(g.get('lower'))}"
                    f"~{price_text(g.get('upper'))}"
                    f" n={g.get('grid_count')}"
                    f" lev={str(g.get('suggested_leverage')) + 'x' if g.get('suggested_leverage') else 'REJECT'}"
                    f" liqCompat={g.get('leverage_compatibility_pass')}"
                    f" atr={pct_text(g.get('atr_pct'))}"
                    f" headroom={pct_text(g.get('leverage_extra_headroom_pct'))}"
                    f" survive={g.get('survival_pass')}"
                )

            if r["status"] == "NO_SIGNAL":
                print(
                    f"{base:<5} NO_SIGNAL    "
                    f"close={price_text(r['latest_close'])} "
                    f"hist={r.get('history_mode')}"
                )
            else:
                t = r.get("expected_target") or {}
                print(
                    f"{base:<5} "
                    f"{r['status']:<12} "
                    f"now={price_text(r['latest_close'])} "
                    f"target={price_text(t.get('target_base'))}"
                    f"~{price_text(t.get('target_high'))} "
                    f"space={pct_text(t.get('expected_base_pct'))}"
                    f"~{pct_text(t.get('expected_high_pct'))} "
                    f"support={price_text(t.get('nearest_support'))} "
                    f"resist={price_text(t.get('nearest_resistance'))} "
                    f"1Dsoft={r['1d_soft']} "
                    f"1D={r['1d_confirm']} "
                    f"4H={r['4h_structure']} "
                    f"1H={r.get('1h_entry')} "
                    f"15m={r.get('15m_entry')} "
                    f"hist={r.get('history_mode')}"
                    f"{grid_text}"
                )

            notify_status_change(r, state)

        except Exception as e:
            errors.append((base, str(e)))
            print(f"{base:<5} ERROR {e}")
            results.append({
                "base": base,
                "contract": contract,
                "status": "ERROR",
                "error": str(e),
            })

        time.sleep(0.15)

    print("\nSCAN HK STOCK 2560 | FIXED 25-STOCK POOL")
    for s in hk_candidates:
        base=s["code"]
        gate_symbol=s["gate_symbol"]
        name_zh=s.get("name_zh") or HK_CORE_CODES.get(base,base)
        label=f"{base} {name_zh}"
        try:
            r=analyze_hk_stock(base,gate_symbol,name_zh)
            results.append(r)
            if r.get("status") == "WAIT_HISTORY":
                print(f"{label:<28} WAIT_HISTORY 1D={r.get('history_1d_bars')} 1H={r.get('history_1h_bars')} 15m={r.get('history_15m_bars')}")
            elif r["status"] == "NO_SIGNAL":
                print(f"{label:<28} NO_SIGNAL close={price_text(r.get('latest_close'))}")
            else:
                tg=r.get("expected_target") or {}
                print(
                    f"{label:<28} {r['status']:<12} "
                    f"now={price_text(r.get('latest_close'))} "
                    f"target={price_text(tg.get('target_base'))}~{price_text(tg.get('target_high'))} "
                    f"space={pct_text(tg.get('expected_base_pct'))}~{pct_text(tg.get('expected_high_pct'))} "
                    f"support={price_text(tg.get('nearest_support'))} "
                    f"resist={price_text(tg.get('nearest_resistance'))} "
                    f"1D={r.get('1d_strategy')} 1H={r.get('1h_wave')} 15m={r.get('15m_entry')}"
                )
            notify_status_change(r,state)
        except Exception as e:
            errors.append((label,str(e)))
            print(f"{label:<28} ERROR {e}")
            results.append({"base":base,"name_zh":name_zh,"display":label,"group":"HK_STOCK","status":"ERROR","error":str(e)})
        time.sleep(0.10)

    RESULT_FILE.write_text(
        json.dumps(
            {
                "generated_utc": now_iso(),
                "rule_version": "2560_FINAL_2026_09_28_HK_FIXED_POOL_UTF8",
                "hk_gate_universe_count": len(hk_universe),
                "hk_fixed_pool_configured": len(HK_FIXED_CODES),
                "hk_fixed_pool_available": len(hk_candidates),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    save_state(state)

    counts = Counter(r.get("status") for r in results)
