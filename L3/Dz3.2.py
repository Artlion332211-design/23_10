a = [12, 3, 4, 10]  #[10, 12, 3, 4]
b = [1]  # [1]
c = []  #[]
d = [12, 3, 4, 10, 8] #[8, 12, 3, 4, 10
z = a.pop()
x = b.pop()
v = d.pop()
n = len(c)
lis2 = d
lis1 = b                       #print(z)
lis = a
lis2.insert(0,v)
lis1.insert(0,x)
lis.insert(0,z)
print(lis)
print(lis1)
if n == 0:
    print([])
print(lis2)
