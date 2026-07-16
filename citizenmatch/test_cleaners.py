

from cleaners import clean_name, clean_street, clean_state, clean_zip, similarity


def test_clean_name():
    tests = [
        ("John A. Smith",      "JOHN A SMITH"),
        ("  MARY  jane DOE  ", "MARY JANE DOE"),
        ("Dr. Robert Jones",   "ROBERT JONES"),
        ("MRS. SALLY O'NEIL",  "SALLY O'NEIL"),
        ("Sgt. Mike Davis Jr", "MIKE DAVIS JR"),
        ("José García",        "JOSE GARCIA"),
        (None,                 None),
        ("",                   None),
    ]
    print("=== clean_name ===")
    all_pass = True
    for raw, expected in tests:
        result = clean_name(raw)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_pass = False
        print(f"  {status} clean_name({raw!r}) → {result!r}  (expected {expected!r})")
    return all_pass


def test_clean_street():
    tests = [
        # (inputs,                                     expected)
        (("123 West Main Street Apt 4",),              "123 W MAIN ST APT 4"),
        (("123 W MAIN ST", "#4"),                      "123 W MAIN ST APT 4"),
        (("456 North Boulevard",),                     "456 N BLVD"),
        (("P.O. Box 789",),                            "PO BOX 789"),
        (("P O BOX 100",),                             "PO BOX 100"),
        (("789 Southeast Parkway Suite 200",),         "789 SE PKWY STE 200"),
        (("100 East Highway 66, Apt. 3B",),            "100 E HWY 66 APT 3B"),
        ((None,),                                      None),
        (("",),                                        None),
    ]
    print("\n=== clean_street ===")
    all_pass = True
    for parts, expected in tests:
        result = clean_street(*parts)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_pass = False
        print(f"  {status} clean_street{parts} → {result!r}  (expected {expected!r})")
    return all_pass


def test_clean_state():
    tests = [
        ("Oklahoma",  "OK"),
        ("ok",        "OK"),
        ("TX",        "TX"),
        ("Ca.",       "CA"),
        (None,        None),
    ]
    print("\n=== clean_state ===")
    all_pass = True
    for raw, expected in tests:
        result = clean_state(raw)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_pass = False
        print(f"  {status} clean_state({raw!r}) → {result!r}  (expected {expected!r})")
    return all_pass


def test_clean_zip():
    tests = [
        ("73102",      "73102"),
        ("73102-4455", "73102"),
        ("7310",       "7310"),
        ("OK 73102",   "73102"),
        (None,         None),
        ("",           None),
    ]
    print("\n=== clean_zip ===")
    all_pass = True
    for raw, expected in tests:
        result = clean_zip(raw)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_pass = False
        print(f"  {status} clean_zip({raw!r}) → {result!r}  (expected {expected!r})")
    return all_pass


def test_similarity():
    tests = [
        ("JOHN SMITH",   "JOHN SMITH",    100),
        ("JOHN SMITH",   "JON SMITH",     None),  
        ("JOHN SMITH",   "JANE DOE",      None),  
        (None,           "JOHN",          0),
    ]
    print("\n=== similarity ===")
    all_pass = True
    for a, b, expected in tests:
        result = similarity(a, b)
        if expected is not None:
            ok = result == expected
        elif a and b and "SMITH" in a and "SMITH" in b:
            ok = result > 80
        else:
            ok = result < 50
        status = "✅" if ok else "❌"
        if not ok:
            all_pass = False
        print(f"  {status} similarity({a!r}, {b!r}) → {result}")
    return all_pass


if __name__ == "__main__":
    results = [
        test_clean_name(),
        test_clean_street(),
        test_clean_state(),
        test_clean_zip(),
        test_similarity(),
    ]

    print("\n" + "=" * 40)
    if all(results):
        print("ALL TESTS PASSED ✅")
    else:
        print("SOME TESTS FAILED ❌")