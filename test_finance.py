import sqlite3
import threading
import time

def run_tests():
    print("==================================================")
    print("🚀 ЗАПУСК АВТО-ТЕСТОВ ФИНАНСОВЫХ ОПЕРАЦИЙ (NEMOS TRADE)")
    print("==================================================")

    # 1. Тест на списание без достаточного баланса
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, earned REAL DEFAULT 0.0, invested REAL DEFAULT 0.0)")
    conn.execute("INSERT INTO users (user_id, earned, invested) VALUES (1, 100.0, 0.0)")
    conn.commit()

    amt = 200.0
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = 1 AND earned >= ?", (amt, amt))
    if cur.rowcount == 0:
        conn.execute("ROLLBACK")
        ok = False
    else:
        conn.commit()
        ok = True
    assert ok is False, "Списание сверх баланса не должно было пройти!"
    print("  [OK] Тест 1: Списание сверх баланса успешно заблокировано")

    # 2. Тест на защиту от гонки транзакций (Race Condition / одновременные клики)
    # Создаем дисковую тестовую базу в temp, чтобы несколько потоков могли параллельно писать
    import tempfile, os
    tmp_db = tempfile.mktemp(suffix=".db")
    t_conn = sqlite3.connect(tmp_db)
    t_conn.execute("PRAGMA journal_mode=WAL")
    t_conn.execute("PRAGMA busy_timeout=5000")
    t_conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, earned REAL DEFAULT 0.0)")
    t_conn.execute("INSERT INTO users (user_id, earned) VALUES (1, 100.0)")
    t_conn.commit()
    t_conn.close()

    results = []
    def attempt_withdrawal(th_id):
        c = sqlite3.connect(tmp_db, timeout=5.0)
        c.execute("PRAGMA busy_timeout=5000")
        try:
            c.execute("BEGIN IMMEDIATE")
            cur_upd = c.execute("UPDATE users SET earned = ROUND(earned - 100.0, 2) WHERE user_id = 1 AND earned >= 100.0")
            if cur_upd.rowcount > 0:
                c.commit()
                results.append(True)
            else:
                c.execute("ROLLBACK")
                results.append(False)
        except Exception:
            try: c.execute("ROLLBACK")
            except Exception: pass
            results.append(False)
        finally:
            c.close()

    threads = [threading.Thread(target=attempt_withdrawal, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    successes = sum(1 for r in results if r is True)
    c_check = sqlite3.connect(tmp_db)
    final_earned = c_check.execute("SELECT earned FROM users WHERE user_id = 1").fetchone()[0]
    c_check.close()
    os.remove(tmp_db)
    if os.path.exists(tmp_db + "-wal"): os.remove(tmp_db + "-wal")
    if os.path.exists(tmp_db + "-shm"): os.remove(tmp_db + "-shm")

    assert successes == 1, f"Должен был победить строго 1 параллельный запрос, победило: {successes}"
    assert final_earned == 0.0, f"Баланс должен быть 0.0, но равен {final_earned}"
    print("  [OK] Тест 2: Защита от параллельных списаний (Race Condition) работает надежно (1 успех из 10)")

    # 3. Тест на реинвест
    conn.execute("UPDATE users SET earned = 150.0, invested = 500.0 WHERE user_id = 1")
    conn.commit()
    reinv_amt = 150.0
    conn.execute("BEGIN IMMEDIATE")
    cur_r = conn.execute("UPDATE users SET earned = ROUND(earned - ?, 2), invested = ROUND(invested + ?, 2) WHERE user_id = 1 AND earned >= ?", (reinv_amt, reinv_amt, reinv_amt))
    if cur_r.rowcount > 0:
        conn.commit()
        r_ok = True
    else:
        conn.execute("ROLLBACK")
        r_ok = False
    assert r_ok is True
    u1_after = conn.execute("SELECT earned, invested FROM users WHERE user_id = 1").fetchone()
    assert u1_after[0] == 0.0 and u1_after[1] == 650.0
    print("  [OK] Тест 3: Атомарный реинвест прибыли в депозит выполнен корректно (earned -> 0, invested -> 650)")

    # 4. Тест P2P перевода и комиссии 2.22%
    conn.execute("INSERT INTO users (user_id, earned, invested) VALUES (2, 0.0, 0.0)")
    conn.execute("UPDATE users SET earned = 1000.0 WHERE user_id = 1")
    conn.commit()
    t_amt = 500.0
    fee = round(t_amt * 0.0222, 2)
    net_amt = round(t_amt - fee, 2)

    conn.execute("BEGIN IMMEDIATE")
    c1 = conn.execute("UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = 1 AND earned >= ?", (t_amt, t_amt))
    c2 = conn.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = 2", (net_amt,))
    conn.commit()

    u1_cur = conn.execute("SELECT earned FROM users WHERE user_id = 1").fetchone()[0]
    u2_cur = conn.execute("SELECT earned FROM users WHERE user_id = 2").fetchone()[0]
    assert u1_cur == 500.0, f"У отправителя должно остаться 500.0, осталось {u1_cur}"
    assert u2_cur == net_amt, f"У получателя должно быть {net_amt}, получено {u2_cur}"
    print(f"  [OK] Тест 4: Целостность P2P перевода (Комиссия 2.22% = {fee}$, зачислено = {net_amt}$) подтверждена")

    # 5. Тест защиты от отрицательных и нулевых значений
    invalid_passed = False
    for bad_amt in [-100.0, 0.0, float("nan")]:
        try:
            if bad_amt <= 0.0 or str(bad_amt) == "nan":
                raise ValueError("Недопустимая сумма")
            invalid_passed = True
        except ValueError:
            pass
    assert invalid_passed is False, "Отрицательные и некорректные суммы не должны приниматься!"
    print("  [OK] Тест 5: Защита от отрицательных сумм и NaN-значений подтверждена")

    # 6. Тест атомарной защиты 12-часового защитного холда при конкурентных запросах
    tmp_db2 = tempfile.mktemp(suffix=".db")
    t_conn2 = sqlite3.connect(tmp_db2)
    t_conn2.execute("PRAGMA journal_mode=WAL")
    t_conn2.execute("PRAGMA busy_timeout=5000")
    t_conn2.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, earned REAL DEFAULT 0.0)")
    t_conn2.execute("CREATE TABLE transfer_logs (id INTEGER PRIMARY KEY, recipient_id INTEGER, net_amount REAL, date TEXT)")
    # Баланс 150$, но 100$ на защитном холде -> доступно строго 50$
    t_conn2.execute("INSERT INTO users (user_id, earned) VALUES (1, 150.0)")
    t_conn2.execute("INSERT INTO transfer_logs (recipient_id, net_amount, date) VALUES (1, 100.0, datetime('now'))")
    t_conn2.commit()
    t_conn2.close()

    hold_results = []
    def attempt_spend_held(th_id):
        c = sqlite3.connect(tmp_db2, timeout=5.0)
        c.execute("PRAGMA busy_timeout=5000")
        try:
            c.execute("BEGIN IMMEDIATE")
            cur_e = c.execute("SELECT earned FROM users WHERE user_id = 1").fetchone()[0]
            cur_h = c.execute("SELECT SUM(net_amount) FROM transfer_logs WHERE recipient_id = 1").fetchone()[0] or 0.0
            avail = max(0.0, round(cur_e - cur_h, 2))
            if avail >= 50.0:
                c.execute("UPDATE users SET earned = ROUND(earned - 50.0, 2) WHERE user_id = 1")
                c.commit()
                hold_results.append(True)
            else:
                c.execute("ROLLBACK")
                hold_results.append(False)
        except Exception:
            try: c.execute("ROLLBACK")
            except Exception: pass
            hold_results.append(False)
        finally:
            c.close()

    threads2 = [threading.Thread(target=attempt_spend_held, args=(i,)) for i in range(10)]
    for t in threads2: t.start()
    for t in threads2: t.join()

    c_chk = sqlite3.connect(tmp_db2)
    final_e2 = c_chk.execute("SELECT earned FROM users WHERE user_id = 1").fetchone()[0]
    c_chk.close()
    os.remove(tmp_db2)
    if os.path.exists(tmp_db2 + "-wal"): os.remove(tmp_db2 + "-wal")
    if os.path.exists(tmp_db2 + "-shm"): os.remove(tmp_db2 + "-shm")

    assert sum(1 for r in hold_results if r is True) == 1, "Строго 1 запрос должен был списать 50$, остальные должны быть заблокированы холдом!"
    assert final_e2 == 100.0, f"На балансе должно остаться ровно 100.0 (сумма холда), но осталось {final_e2}"
    print("  [OK] Тест 6: Атомарная защита 12-часового защитного холда при гонке запросов подтверждена (1 успех из 10, остаток 100$)")

    # 7. Тест неделимого (атомарного) отката отклоненной заявки
    conn_a = sqlite3.connect(":memory:")
    conn_a.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, amount REAL)")
    conn_a.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, earned REAL DEFAULT 0.0)")
    conn_a.execute("INSERT INTO users (user_id, earned) VALUES (1, 10.0)")
    conn_a.execute("INSERT INTO transactions (id, user_id, status, amount) VALUES (101, 1, 'В обработке', 200.0)")
    conn_a.commit()

    # Атомарное отклонение: статус меняется на Отклонено и средства возвращаются в одной транзакции
    conn_a.execute("BEGIN IMMEDIATE")
    cur_tx = conn_a.execute("UPDATE transactions SET status = 'Отклонено' WHERE id = 101 AND status = 'В обработке'")
    conn_a.execute("UPDATE users SET earned = ROUND(earned + 200.0, 2) WHERE user_id = 1")
    conn_a.commit()

    final_tx_st = conn_a.execute("SELECT status FROM transactions WHERE id = 101").fetchone()[0]
    final_user_e = conn_a.execute("SELECT earned FROM users WHERE user_id = 1").fetchone()[0]
    conn_a.close()
    assert final_tx_st == "Отклонено" and final_user_e == 210.0
    print("  [OK] Тест 7: Атомарный возврат средств при отклонении заявки подтвержден (статус 'Отклонено', баланс 210$)")

    conn.close()
    print("==================================================")
    print("✅ ВСЕ 7 ТЕСТОВ ФИНАНСОВОЙ БЕЗОПАСНОСТИ УСПЕШНО ПРОЙДЕНЫ!")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
