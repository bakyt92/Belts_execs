#include <iostream>
using namespace std;

int sum(int X, int Y)
{
    return X + Y;
}

int main() {
    int x, y;
    cin >> x >> y;
    cout << sum(x, y);
    return 0;
}