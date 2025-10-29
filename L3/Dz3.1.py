a = float(input("Введіть чисо:"))
b = input("Введіть математічну дію:")
c = float(input("Введіть чисо:"))
if b =="+":
    rezult=a + c
    print (int(rezult))
if b =="-":
    rezult = a-c
    print (int(rezult))
if b =="*":
    rezult = a * c
    print (int(rezult))
if b =="/":
    if c == 0:
        print("Ділити на 0 не можна")
    else:
        rezult = a / c
        print(int(rezult))
