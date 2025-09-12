array_input = input("Введите первые 11 цифр ИИН: ")

IIN_ARr = []

def Calculate():
    for IIN_digit in array_input:
        IIN_ARr.append(int(IIN_digit))

    Ves_Arr = [1,2,3,4,5,6,7,8,9,10,11]

    pass_count = 1 

    Sum = 0
    for i in range(len(IIN_ARr)):
        half_sum = IIN_ARr[i] * Ves_Arr[i]
        Sum += half_sum

    print("Сумма произведений (проход", pass_count, "):", Sum)

    
    C = Sum // 11

    K = Sum - (C * 11)

    if K < 10:
        print("Контрольный разряд:", K)
    else:

        pass_count += 1
        print("Первый проход дал K =", K, "→ делаем сдвиг массива весов на 1 позицию вправо")

        # сдвиг на одну позицию вправо
        Ves_Arr = [Ves_Arr[-1]] + Ves_Arr[:-1]

        Sum = 0
        for i in range(len(IIN_ARr)):
            half_sum = IIN_ARr[i] * Ves_Arr[i]
            Sum += half_sum

        print("Сумма произведений (проход", pass_count, "):", Sum)

        C = Sum // 11
        K = Sum - (C * 11)

        if K < 10:
            print("Контрольный разряд:", K)
        else:
            print("После второго прохода снова K =", K, "→ контрольный разряд не определяется")

    print("Количество проходов:", pass_count)
    print("Изменённый ИИН:", IIN_ARr + [K])


Calculate()
