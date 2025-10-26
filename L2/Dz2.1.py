
n = int(input("Введи 4-значне число: "))
a, b = divmod(n,1000)
print(a)
a, b = divmod(b,100)
print(a)
a, b = divmod(b,10)
print(a)
print(b)