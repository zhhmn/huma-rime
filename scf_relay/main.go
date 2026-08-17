package main

import "github.com/tencentyun/scf-go-lib/cloudfunction"

func main() {
	cloudfunction.Start(handleRequest)
}
