while True:
    a = float(input("Введіть число: "))
    b = input("Введіть математичну дію (+, -, *, /): ")
    c = float(input("Введіть число: "))
    if b == "+":
        result = a + c
        print("Результат:", int(result))
    elif b == "-":
        result = a - c
        print("Результат:", int(result))
    elif b == "*":
        result = a * c
        print("Результат:", int(result))
    elif b == "/":
        if c == 0:
            print("Ділити на 0 не можна")
        else:
            result = a / c
            print("Результат:", int(result))
    else:
        print("Невідома дія, використайте +, -, * або /")
    # запит: чи продовжувати?
    cont = input("Продовжити? (y для так, будь-що інше – ні): ")
    if cont != "y":
        print("Калькулятор завершив роботу.")
        break
