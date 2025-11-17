import string
import keyword
name = input("Введіть можливе ім'я змінної: ")

def is_valid_variable(name: str) -> bool:
    if not name:
        return False


    if name in keyword.kwlist:
        return False


    if name[0].isdigit():
        return False


    if any(ch.isupper() for ch in name):
        return False


    if " " in name:
        return False


    for ch in name:
        if ch in string.punctuation and ch != "_":
            return False


    if name.count("_") > 1:
        return False


    for ch in name:
        if ch.isalpha():
            if not ch.islower():
                return False
        elif ch.isdigit():
            continue
        elif ch == "_":
            continue
        else:
            return False
    return True
print(is_valid_variable(name))
