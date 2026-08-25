#include <iostream>
#include <vector>
#include <string> 
using namespace std;

bool Contains(vector<string> words, string w)
{
    for (auto s: words) {
        if (s == w) {
            return true;
        }
    }
    return false;
}

int main() {
    cout << Contains({"water", "air", "terre"}, "air");
    return 0;
}